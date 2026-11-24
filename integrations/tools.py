"""Tools connected apps offer the assistant, and calling them.

An app describes its tools in a manifest (integrations.api, docs/integrations.md):
    {"callback": "/saaschat/tools",
     "tools": [{"name": "get_my_orders", "description": "...", "kind": "read",
                "needs_user": true, "parameters": {"type": "object", "properties": {...}}}]}
When the model calls one, saaschat POSTs to the app's address + callback:
    {"tool": "get_my_orders", "arguments": {...}, "user_token": "<the user's token>" | null,
     "scope": ..., "page": ...}
with X-Saaschat-Timestamp and X-Saaschat-Signature: sha256=hex(HMAC(secret, "<timestamp>.<body>")).
The app checks the signature and the user token it issued, acts as that user, and answers
with JSON, which goes to the model. Tools with needs_user are only offered to users signed in
by that app; others to every visitor of the workspace's widget.

The Moodle plugin's student tools (grades, completion, deadlines) use their own older call
(widget.student_tools) behind the same interface.
"""
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .models import Integration

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_TOOLS = 20
MAX_RESULT_CHARS = 12000
NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
RESERVED = {"search_content", "search_course"}
SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_content",
    "description": "Search the content for the section the user is in (pages, documents, records sent by the "
                   "app). Returns numbered passages to cite like [3].",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to look for, in plain words"}},
        "required": ["query"], "additionalProperties": False}}}


class ManifestError(ValueError):
    pass


def validate_manifest(data) -> dict:
    """The manifest as stored, or ManifestError saying what is wrong."""
    if not isinstance(data, dict):
        raise ManifestError("Expected a JSON object")
    callback = data.get("callback", "")
    if not isinstance(callback, str) or (callback and not callback.startswith("/")) or len(callback) > 300:
        raise ManifestError('"callback" must be a path on the app\'s address, e.g. "/saaschat/tools"')
    tools = data.get("tools", [])
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ManifestError(f'"tools" must be a list of at most {MAX_TOOLS} tools')
    clean, seen = [], set()
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ManifestError(f"Tool {i + 1} is not an object")
        name = tool.get("name")
        if not isinstance(name, str) or not NAME.match(name) or name in RESERVED or name in seen:
            raise ManifestError(f"Tool {i + 1}: name must be unique, lower_case_with_underscores, and not "
                                f"{', '.join(sorted(RESERVED))}")
        description = tool.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 1500:
            raise ManifestError(f"{name}: a description (up to 1500 characters) is required")
        params = tool.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(params, dict) or params.get("type") != "object" or len(json.dumps(params)) > 5000:
            raise ManifestError(f'{name}: "parameters" must be a JSON Schema object ({{"type": "object", ...}})')
        kind = tool.get("kind", "read")
        if kind not in ("read", "action"):
            raise ManifestError(f'{name}: "kind" is "read" or "action"')
        seen.add(name)
        clean.append({"name": name, "description": description.strip(), "parameters": params, "kind": kind,
                      "needs_user": bool(tool.get("needs_user", True))})
    if clean and not callback:
        raise ManifestError('Tools need a "callback" path')
    return {"callback": callback, "tools": clean}


@dataclass
class Result:
    text: str        # for the model
    outline: str     # for the brain map: what came back, never the data
    ms: int
    ok: bool
    action: bool = False


def _result_text(data) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " …(cut short)"


def _outline(data) -> str:
    if isinstance(data, dict) and data.get("error"):
        return f"Error: {str(data['error'])[:200]}"
    if isinstance(data, list):
        return f"{len(data)} item{'s' if len(data) != 1 else ''}"
    if isinstance(data, dict):
        lists = {k: len(v) for k, v in data.items() if isinstance(v, list)}
        if lists:
            return ", ".join(f"{n} {k}" for k, n in list(lists.items())[:3])
        return f"{len(data)} field{'s' if len(data) != 1 else ''}"
    return "Done"


class AppTool:
    """One tool from an app's manifest."""

    def __init__(self, integration: Integration, spec: dict, visitor=None):
        self.integration, self.spec, self.visitor = integration, spec, visitor

    @property
    def name(self) -> str:
        return self.spec["name"]

    def definition(self) -> dict:
        description = self.spec["description"]
        if self.spec["kind"] == "action":
            description += (" This changes something: first tell the user exactly what you will do and only call "
                            "it after they clearly agree.")
        return {"type": "function", "function": {"name": self.name, "description": description,
                                                 "parameters": self.spec["parameters"]}}

    async def call(self, args: dict) -> Result:
        mine = self.visitor is not None and self.visitor.integration.pk == self.integration.pk
        body = json.dumps({
            "tool": self.name, "arguments": args, "user_token": self.visitor.token if mine else None,
            "scope": self.visitor.scope if mine else None, "page": self.visitor.page if mine else None,
        }).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(self.integration.secret.encode(), stamp.encode() + b"." + body, hashlib.sha256)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
                resp = await client.post(self.integration.callback_url, content=body, headers={
                    "Content-Type": "application/json", "Accept": "application/json",
                    "X-Saaschat-Timestamp": stamp, "X-Saaschat-Signature": f"sha256={signature.hexdigest()}",
                    "X-Saaschat-Integration": self.integration.key,
                })
            try:
                data = resp.json()
            except ValueError:
                data = {"error": "The app's answer wasn't JSON"} if resp.status_code < 300 else {}
            if resp.status_code >= 300:
                error = data.get("error") if isinstance(data, dict) else None
                data = {"error": error or f"The app answered with HTTP {resp.status_code}"}
        except httpx.HTTPError as exc:
            logger.warning("Tool %s on %s failed: %s", self.name, self.integration.callback_url, exc)
            data = {"error": "The app could not be reached"}
        ms = int((time.monotonic() - started) * 1000)
        failed = isinstance(data, dict) and bool(data.get("error"))
        return Result(_result_text(data), _outline(data), ms, not failed, self.spec["kind"] == "action")


class MoodleTool:
    """One of the Moodle plugin's student tools (widget.student_tools)."""

    def __init__(self, definition: dict, lookups):
        self._definition, self.lookups = definition, lookups

    @property
    def name(self) -> str:
        return self._definition["function"]["name"]

    def definition(self) -> dict:
        return self._definition

    async def call(self, args: dict) -> Result:
        from widget import student_tools

        data, ms = await self.lookups.lookup(self.name)
        inner = data.get("data") if isinstance(data, dict) else None
        failed = not isinstance(data, dict) or bool(data.get("error")) or (isinstance(inner, dict)
                                                                         and bool(inner.get("error")))
        return Result(student_tools.result_text(data), student_tools.outline(self.name, data), ms, not failed)


@dataclass
class ToolSet:
    """The tools offered for one reply."""
    tools: dict          # name -> AppTool | MoodleTool
    search_name: str     # the content search tool's name, "" when there is no scoped content
    moodle: bool         # use the Moodle student-tools instructions

    def definitions(self) -> list[dict]:
        defs = [t.definition() for t in self.tools.values()]
        if self.search_name == "search_content":
            defs.append(SEARCH_TOOL)
        elif self.search_name == "search_course":
            from widget import student_tools

            defs.append(next(d for d in student_tools.DEFINITIONS if d["function"]["name"] == "search_course"))
        return defs

    def __bool__(self):
        return bool(self.tools) or bool(self.search_name)


def for_widget(widget, visitor) -> ToolSet | None:
    """Tools for a widget reply: the visitor's own app's tools (and its content search when
    they are in a scope), plus tools any of the workspace's apps offer to every visitor."""
    tools, search_name, moodle = {}, "", False
    if visitor is not None and visitor.integration.kind == Integration.Kind.MOODLE:
        from widget.student_tools import DEFINITIONS, MOODLE_TOOLS, StudentTools

        student = {"site": visitor.site, "course": visitor.scope}
        if visitor.scope and visitor.integration.tools and StudentTools.available(widget, visitor.integration,
                                                                                student):
            lookups = StudentTools(visitor.integration, student, visitor.token)
            for d in DEFINITIONS:
                if d["function"]["name"] in MOODLE_TOOLS:
                    tools[d["function"]["name"]] = MoodleTool(d, lookups)
            search_name, moodle = "search_course", True
    elif visitor is not None and visitor.scope:
        search_name = "search_content" if visitor.integration.tools else ""
    for integration in Integration.objects.filter(tenant=widget.tenant, enabled=True, tools=True,
                                                  kind=Integration.Kind.APP).exclude(site_url=""):
        mine = visitor is not None and visitor.integration.pk == integration.pk
        for spec in integration.manifest_tools:
            if spec["name"] in tools or (spec.get("needs_user", True) and not mine):
                continue
            tools[spec["name"]] = AppTool(integration, spec, visitor if mine else None)
    toolset = ToolSet(tools, search_name, moodle)
    return toolset if toolset else None
