"""Tools the assistant can use for a Moodle student on a course's pages: look up the student's
own grades, completion and deadlines (from the Moodle plugin's student.php, as that student)
and search the course's content.

The plugin is called with the shared code and the student's own signed token, so saaschat
can only ever look up the student who is asking, and Moodle decides what they may see.
Results go to the model only; the brain map records what was looked up, not the data.
"""
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 15  # seconds per lookup
MAX_RESULT_CHARS = 12000  # of JSON given to the model per call

DEFINITIONS = [
    {"type": "function", "function": {
        "name": "get_my_grades",
        "description": "The student's own grades in this course: each grade item they can see with its grade, "
                       "range, percentage, weight and feedback, the course total, and how much of the course "
                       "weight is already graded. Use for questions about their grades, marks, scores, feedback "
                       "or whether they are passing.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_my_completion",
        "description": "The student's own completion in this course: the course's completion criteria and which "
                       "they have met, and each tracked activity with its status and what it requires (view, "
                       "submit, receive a pass grade ...). Use for questions about what they have done, what is "
                       "left, or what they need to complete the course.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_my_deadlines",
        "description": "What the student still has to do in this course, with due dates (their own extensions "
                       "and overrides included) and whether each is overdue. Finished work is not listed. Use for "
                       "questions about what is due, deadlines, or what to do next.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_course",
        "description": "Search this course's content (overview, schedule, activities, pages, files, forum "
                       "posts). Returns numbered passages to cite like [3]. Use for anything about the course "
                       "itself, e.g. what an assignment asks for or how it is graded.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words"}},
            "required": ["query"], "additionalProperties": False}}},
]
MOODLE_TOOLS = {"get_my_grades": "grades", "get_my_completion": "completion", "get_my_deadlines": "deadlines"}


class StudentTools:
    """Moodle lookups for one signed-in student in one course (the workspace's Moodle
    integration, integrations.tools.MoodleTool)."""

    def __init__(self, integration, student: dict, token: str):
        self.url = student["site"].rstrip("/") + "/local/saaschat/student.php"
        self.code = integration.secret
        self.token = token

    @staticmethod
    def available(widget, integration, student: dict | None) -> bool:
        """Only to the student's own Moodle site, and only if it is an allowed website."""
        return bool(integration.enabled and integration.content and integration.tools and student
                    and student.get("course") and student.get("site") and widget.allows_site(student["site"]))

    async def lookup(self, tool: str) -> tuple[dict, int]:
        """(result for the model, milliseconds). Never raises: failures come back as {"error": ...}."""
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
                resp = await client.post(self.url, json={"token": self.token, "tool": MOODLE_TOOLS[tool]},
                                         headers={"X-Saaschat-Code": self.code, "Accept": "application/json"})
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code != 200:
                data = {"error": (data.get("error") if isinstance(data, dict) else None)
                        or f"Moodle answered with HTTP {resp.status_code}"}
        except httpx.HTTPError as exc:
            logger.warning("Moodle student lookup %s at %s failed: %s", tool, self.url, exc)
            data = {"error": "Moodle could not be reached for this lookup"}
        return data, int((time.monotonic() - started) * 1000)


def result_text(data) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + ' …(cut short)'


def outline(tool: str, data: dict) -> str:
    """What a lookup returned, without the student's data (for the brain map)."""
    if not isinstance(data, dict):
        return "Unexpected answer"
    if data.get("error"):
        return f"Error: {data['error']}"
    d = data.get("data") or {}
    if isinstance(d, dict) and d.get("error"):
        return f"Moodle: {d['error']}"
    if tool == "get_my_grades":
        items = len(d.get("items") or [])
        total = "course total included" if "grade" in (d.get("course_total") or {}) else "no course total shown"
        return f"{items} grade item{'s' if items != 1 else ''}, {total}"
    if tool == "get_my_completion":
        if not d.get("tracking"):
            return "Completion tracking is off for this course"
        criteria = len((d.get("course") or {}).get("criteria") or [])
        return f"{d.get('activities_summary', '')}; {criteria} course criteri{'a' if criteria != 1 else 'on'}"
    if tool == "get_my_deadlines":
        todo = d.get("to_do") or []
        overdue = sum(1 for e in todo if e.get("overdue"))
        return f"{len(todo)} thing{'s' if len(todo) != 1 else ''} to do" + (f", {overdue} overdue" if overdue else "")
    return "Done"
