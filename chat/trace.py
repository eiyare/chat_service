"""The brain map: a record of every step that produced a reply, saved on the reply as
Message.trace and shown in the workspace admin (Knowledge base > Brain map).

Each step is {"key", "label", "status", "summary", "ms", "detail"}. Status says what the
step did with the question:
  "pass"  ran and handed on to the next step     "hit"  answered the question here
  "miss"  looked but found nothing               "skip" didn't run (the reason is the summary)
  "fail"  went wrong                             "info" context, not a decision
"""
import time

EXCERPT = 400  # characters of passage text kept for candidates


def excerpt(text: str | None, limit: int = EXCERPT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


class Trace:
    def __init__(self):
        self.started = time.monotonic()
        self.steps: list[dict] = []

    def add(self, key: str, label: str, status: str, summary: str = "", ms: int | None = None, **detail) -> dict:
        step = {"key": key, "label": label, "status": status, "summary": summary, "ms": ms,
                "detail": {k: v for k, v in detail.items() if v not in (None, "", [], {})}}
        self.steps.append(step)
        return step

    @staticmethod
    def clock() -> float:
        return time.monotonic()

    @staticmethod
    def since(start: float) -> int:
        return int((time.monotonic() - start) * 1000)
