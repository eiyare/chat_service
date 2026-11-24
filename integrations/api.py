"""The API connected apps call, at /i/<integration key>/... (docs/integrations.md).

Every call sends "Authorization: Bearer <the integration's secret>".

    GET    /i/<key>/                                 who am I: workspace, what's allowed, file types
    PUT    /i/<key>/manifest/                        the tools the app offers (integrations.tools)
    GET    /i/<key>/scopes/                          scopes holding content, with item counts
    GET    /i/<key>/scopes/<scope>/items/            {item key: version} in a scope
    POST   /i/<key>/scopes/<scope>/items/            add or replace one item (multipart file, or JSON text)
    POST   /i/<key>/scopes/<scope>/items/delete/     {"keys": [...]}
    POST   /i/<key>/scopes/<scope>/delete/           everything in the scope
    GET    /i/<key>/documents/                       the workspace's documents (to copy passages)
    GET    /i/<key>/documents/<id>/chunks/           one document's passages

<scope> is the scope's name (e.g. "course:12", "client:acme"), or "shared" for content that
adds to the workspace's knowledge for everyone. The Moodle plugin's older endpoints
(/w/<widget key>/moodle/...) use the same functions (widget.views).
"""
import hmac
import json
import re
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from rag import jobs, services
from rag.models import Document, IndexingRun

from .identity import SCOPE
from .models import Integration
from .tools import ManifestError, validate_manifest

ITEM_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@/-]{0,99}$")
SHARED = "shared"
TEXT_FORMATS = {"md": "md", "markdown": "md", "txt": "txt", "text": "txt"}


def error(message, status):
    return JsonResponse({"error": message}, status=status)


class Denied(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.response = error(message, status)


def _check(integration, request, need: str | None):
    """Raise Denied unless the request carries the integration's secret and the integration
    is allowed ``need`` ("content", "tools" or "share_documents")."""
    sent = request.headers.get("Authorization", "")
    secret = integration.secret if integration else ""
    if integration is None or not integration.enabled or not integration.tenant.is_active:
        raise Http404("Not found")
    if not (secret and sent.startswith("Bearer ") and hmac.compare_digest(sent[7:].encode(), secret.encode())):
        raise Denied("Wrong secret", 403)
    if need and not getattr(integration, need):
        labels = {"content": "Content from the app", "tools": "Tools", "share_documents": "Let the app copy passages"}
        raise Denied(f'"{labels[need]}" is turned off for this integration in saaschat', 409)
    if integration.last_seen_at is None or timezone.now() - integration.last_seen_at > timedelta(minutes=1):
        Integration.objects.filter(pk=integration.pk).update(last_seen_at=timezone.now())


def authorise(request, key, need=None) -> Integration:
    integration = Integration.objects.select_related("tenant").filter(key=key).first()
    _check(integration, request, need)
    return integration


aauthorise = sync_to_async(authorise)


def scope_name(value: str) -> str:
    if value == SHARED:
        return ""
    if not SCOPE.match(value or ""):
        raise Denied('Invalid scope: letters, digits and : _ . - (up to 100), or "shared"', 400)
    return value


def _documents(integration, scope=None):
    docs = Document.objects.filter(integration=integration)
    return docs if scope is None else docs.filter(scope=scope)


# --- Shared by the generic endpoints and the Moodle plugin's older ones ---------------------

async def list_items(integration, scope: str) -> JsonResponse:
    policy = integration.tenant.upload_policy
    items = {k: v async for k, v in _documents(integration, scope).values_list("item_key", "item_version")}
    return JsonResponse({"items": items, "extensions": list(services.app_item_extensions(policy)),
                         "max_file_bytes": policy.max_file_bytes})


async def put_item(integration, scope: str, request) -> JsonResponse:
    """Multipart (key, version, name, file), or JSON {"key", "version", "name", "text", "format"}."""
    policy = integration.tenant.upload_policy
    if request.content_type == "application/json":
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            return error("Invalid JSON", 400)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return error('Expected {"key", "name", "text"} (and optionally "version", "format": "md" or "txt")', 400)
        item_key, version, name = str(body.get("key", "")), str(body.get("version", "")), str(body.get("name", ""))
        ext = TEXT_FORMATS.get(str(body.get("format", "md")).lower())
        if ext is None:
            return error('"format" is "md" or "txt"', 400)
        filename, data = f"item.{ext}", body["text"].encode()
    else:
        item_key, version = request.POST.get("key", ""), request.POST.get("version", "")
        name = request.POST.get("name", "")
        upload = request.FILES.get("file")
        if upload is None:
            return error("No file", 400)
        if upload.size > policy.max_file_bytes:
            return error(f"{upload.name}: larger than the {policy.max_file_mb} MB limit", 413)
        filename, data = upload.name, upload.read()
    if not ITEM_KEY.match(item_key):
        return error("Invalid key: letters, digits and : _ . @ / - (up to 100)", 400)
    try:
        document = await services.store_app_item(integration, scope, item_key, version, name.strip(), filename,
                                                 data)
    except services.UploadRejected as exc:
        return error(str(exc), 400)
    await jobs.aenqueue(document, IndexingRun.Trigger.UPLOAD)
    return JsonResponse({"id": document.pk, "status": document.status}, status=202)


async def delete_items(integration, scope: str, request) -> JsonResponse:
    try:
        keys = json.loads(request.body or b"{}").get("keys")
    except (ValueError, AttributeError):
        keys = None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return error('Expected {"keys": [...]}', 400)
    deleted = 0
    async for document in _documents(integration, scope).filter(item_key__in=keys):
        await services.delete_document(document)
        deleted += 1
    return JsonResponse({"deleted": deleted})


async def delete_scope(integration, scope: str) -> JsonResponse:
    deleted = 0
    async for document in _documents(integration, scope):
        await services.delete_document(document)
        deleted += 1
    return JsonResponse({"deleted": deleted})


def _workspace_documents(integration):
    return Document.objects.filter(tenant=integration.tenant, status=Document.Status.READY, integration__isnull=True)


def list_documents(integration) -> JsonResponse:
    docs = [{"id": d.pk, "name": d.name, "version": f"{d.sha256[:16]}-{int(d.updated_at.timestamp())}"}
            for d in _workspace_documents(integration).order_by("pk")]
    return JsonResponse({"documents": docs})


def document_chunks(integration, pk) -> JsonResponse:
    try:
        doc = _workspace_documents(integration).get(pk=pk)
    except Document.DoesNotExist:
        raise Http404("Document not found")
    chunks = [{"index": c.index, "section": c.section, "text": c.text}
              for c in doc.chunks.order_by("index").only("index", "section", "text")]
    return JsonResponse({"id": doc.pk, "name": doc.name, "chunks": chunks})


# --- Generic endpoints ------------------------------------------------------------------------

def _handles_denied(view):
    """Turn Denied into its JSON response (sync or async view)."""
    import asyncio
    import functools

    if asyncio.iscoroutinefunction(view):
        @functools.wraps(view)
        async def wrapped(*args, **kwargs):
            try:
                return await view(*args, **kwargs)
            except Denied as exc:
                return exc.response
    else:
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except Denied as exc:
                return exc.response
    return wrapped


@login_not_required  # server-to-server: the app authenticates with its secret
@require_GET
@_handles_denied
def info(request, key):
    integration = authorise(request, key)
    policy = integration.tenant.upload_policy
    return JsonResponse({
        "integration": integration.name, "kind": integration.kind, "workspace": integration.tenant.name,
        "allowed": {"signed_users": integration.signed_users, "content": integration.content,
                    "tools": integration.tools, "share_documents": integration.share_documents},
        "extensions": list(services.app_item_extensions(policy)), "max_file_bytes": policy.max_file_bytes,
        "tools": [t["name"] for t in integration.manifest_tools],
    })


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "PUT", "POST"])
@_handles_denied
def manifest(request, key):
    integration = authorise(request, key)
    if request.method == "GET":
        return JsonResponse(integration.manifest or {"callback": "", "tools": []})
    try:
        clean = validate_manifest(json.loads(request.body or b"{}"))
    except ValueError as exc:  # includes ManifestError and bad JSON
        return error(str(exc) if isinstance(exc, ManifestError) else "Invalid JSON", 400)
    integration.manifest, integration.manifest_updated_at = clean, timezone.now()
    integration.save(update_fields=["manifest", "manifest_updated_at", "updated_at"])
    note = "" if integration.tools else ' Saved; "Tools" must be turned on in saaschat before they are used.'
    return JsonResponse({"tools": [t["name"] for t in clean["tools"]], "note": note.strip()})


@login_not_required
@require_GET
@_handles_denied
async def scopes(request, key):
    integration = await aauthorise(request, key, "content")
    from django.db.models import Count

    rows = [r async for r in _documents(integration).values("scope").annotate(items=Count("id")).order_by("scope")]
    return JsonResponse({"scopes": [{"scope": r["scope"] or SHARED, "items": r["items"]} for r in rows]})


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
@_handles_denied
async def items(request, key, scope):
    integration = await aauthorise(request, key, "content")
    name = scope_name(scope)
    if request.method == "GET":
        return await list_items(integration, name)
    return await put_item(integration, name, request)


@login_not_required
@csrf_exempt
@require_POST
@_handles_denied
async def items_delete(request, key, scope):
    integration = await aauthorise(request, key, "content")
    return await delete_items(integration, scope_name(scope), request)


@login_not_required
@csrf_exempt
@require_POST
@_handles_denied
async def scope_delete(request, key, scope):
    integration = await aauthorise(request, key, "content")
    return await delete_scope(integration, scope_name(scope))


@login_not_required
@require_GET
@_handles_denied
def documents(request, key):
    return list_documents(authorise(request, key, "share_documents"))


@login_not_required
@require_GET
@_handles_denied
def chunks(request, key, pk):
    return document_chunks(authorise(request, key, "share_documents"), pk)
