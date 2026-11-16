"""Background processing of documents.

Uploads and rebuilds queue a job on the document (``Document.job``) and return
at once. A worker thread in each server process claims queued jobs from the
database, runs ``services.process`` and records its progress on the document;
the workspace Admin page polls that progress.

- Claiming is a conditional UPDATE, so several server processes never run the
  same job.
- A running job refreshes ``job_heartbeat``. One whose heartbeat is older than
  STALE_AFTER (its process stopped or restarted mid-job) is claimed again.
- With ``RAG_INLINE_PROCESSING`` set, jobs run immediately in the caller.
"""
import asyncio
import contextlib
import logging
import threading
from datetime import timedelta

from asgiref.sync import ThreadSensitiveContext, async_to_sync, sync_to_async
from django.conf import settings
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from .models import Document, IndexingRun

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
STALE_AFTER = timedelta(seconds=90)
IDLE_POLL_SECONDS = 30  # also how often interrupted jobs are looked for

Job = Document.Job


def _inline() -> bool:
    return getattr(settings, "RAG_INLINE_PROCESSING", False)


def _queued(trigger: str) -> dict:
    return dict(job=Job.QUEUED, job_trigger=trigger, job_stage="", job_done=0, job_total=0, job_heartbeat=None)


_IDLE = dict(job="", job_trigger="", job_stage="", job_done=0, job_total=0, job_heartbeat=None)


# --- queueing --------------------------------------------------------------------

async def aenqueue(document: Document, trigger: str) -> bool:
    """Queue processing of ``document`` and refresh it. False if it already has a job."""
    queued = await Document.objects.filter(pk=document.pk, job="").aupdate(**_queued(trigger))
    if queued:
        if _inline():
            await run_job(document.pk)
        else:
            kick()
    with contextlib.suppress(Document.DoesNotExist):
        await document.arefresh_from_db()
    return bool(queued)


def enqueue(document: Document, trigger: str) -> bool:
    """Sync version of ``aenqueue`` (Django admin actions)."""
    queued = Document.objects.filter(pk=document.pk, job="").update(**_queued(trigger))
    if queued:
        if _inline():
            async_to_sync(run_job)(document.pk)
        else:
            kick()
    with contextlib.suppress(Document.DoesNotExist):
        document.refresh_from_db()
    return bool(queued)


# --- running one job ---------------------------------------------------------------

async def _claim(pk: int | None = None) -> int | None:
    """Take the oldest queued (or interrupted) job; returns its document id."""
    now = timezone.now()
    interrupted = Q(job=Job.RUNNING) & (Q(job_heartbeat__lt=now - STALE_AFTER) | Q(job_heartbeat__isnull=True))
    candidates = Document.objects.filter(Q(job=Job.QUEUED) | interrupted)
    if pk is not None:
        candidates = candidates.filter(pk=pk)
    rows = [r async for r in candidates.order_by("pk").values("pk", "job", "job_heartbeat")[:20]]
    for row in rows:
        # Only succeeds if nobody claimed it since we looked.
        same = Document.objects.filter(pk=row["pk"], job=row["job"])
        if row["job_heartbeat"] is None:
            same = same.filter(job_heartbeat__isnull=True)
        else:
            same = same.filter(job_heartbeat=row["job_heartbeat"])
        if await same.aupdate(job=Job.RUNNING, job_stage="", job_done=0, job_total=0, job_heartbeat=now):
            if row["job"] == Job.RUNNING:
                logger.warning("Resuming interrupted processing of document %s", row["pk"])
            return row["pk"]
    return None


def _reporter(pk: int):
    async def report(stage: str, done: int = 0, total: int = 0) -> None:
        await Document.objects.filter(pk=pk, job=Job.RUNNING).aupdate(
            job_stage=stage, job_done=done, job_total=total, job_heartbeat=timezone.now(),
        )
    return report


async def _heartbeat(pk: int) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await Document.objects.filter(pk=pk, job=Job.RUNNING).aupdate(job_heartbeat=timezone.now())


async def run_job(pk: int | None = None) -> bool:
    """Claim and process one job (that document's, or the oldest waiting). False if none."""
    from . import services

    pk = await _claim(pk)
    if pk is None:
        return False
    beat = asyncio.ensure_future(_heartbeat(pk))
    try:
        document = await Document.objects.select_related("tenant").filter(pk=pk).afirst()
        if document is not None:  # deleted meanwhile: nothing to do
            trigger = document.job_trigger or IndexingRun.Trigger.UPLOAD
            await services.process(document, trigger=trigger, progress=_reporter(pk))
    except Exception:
        logger.exception("Processing document %s stopped unexpectedly", pk)
        # A new upload would otherwise show "Processing" for ever; a rebuilt document keeps its old chunks.
        await Document.objects.filter(pk=pk, status=Document.Status.PROCESSING).aupdate(
            status=Document.Status.FAILED, error="Processing stopped unexpectedly. Rebuild to try again.",
        )
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        await Document.objects.filter(pk=pk).aupdate(**_IDLE)
    return True


# --- the worker thread -------------------------------------------------------------

class _Worker(threading.Thread):
    """Runs jobs one at a time on its own event loop, so the server's loop stays free."""

    def __init__(self):
        super().__init__(name="rag-jobs", daemon=True)
        self.loop = asyncio.new_event_loop()
        self.wake: asyncio.Event | None = None

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        self.wake = asyncio.Event()
        while True:
            self.wake.clear()
            try:
                while await self._one():
                    pass
            except Exception:  # e.g. the database is unreachable: try again later
                logger.exception("Document worker error")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), IDLE_POLL_SECONDS)

    @staticmethod
    async def _one() -> bool:
        # Database work for this job gets its own thread and connection, closed afterwards.
        async with ThreadSensitiveContext():
            try:
                return await run_job()
            finally:
                await sync_to_async(connections.close_all)()

    def kick(self):
        if self.wake is not None:  # before that, the worker's first pass picks the job up anyway
            self.loop.call_soon_threadsafe(self.wake.set)


_worker: _Worker | None = None
_lock = threading.Lock()


def start() -> _Worker | None:
    """Start this process's worker (idempotent). Called by the ASGI app at start-up."""
    global _worker
    if _inline():
        return None
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = _Worker()
            _worker.start()
        return _worker


def kick() -> None:
    """Tell the worker there is a job waiting."""
    worker = start()
    if worker is not None:
        worker.kick()
