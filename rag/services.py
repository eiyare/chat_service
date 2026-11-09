"""Document ingestion and knowledge-base statistics."""
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path, PurePath

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.utils import timezone

import llm
from siteconfig import conf

from .chunking import chunk_text
from .extract import ExtractionError, extract_text
from .models import Chunk, Document, IndexingRun, embed_signature

logger = logging.getLogger(__name__)


class UploadRejected(Exception):
    """The file was refused before any processing (bad type, too big, duplicate...)."""


@dataclass
class Report:
    documents: int
    chunks: int
    failed: int
    processing: int
    total_bytes: int
    stale: int = 0  # ready documents embedded with outdated settings (need reindexing)


def file_extension(name: str) -> str:
    return PurePath(name).suffix.lower().lstrip(".")


def validate_upload(name: str, size: int, policy) -> str:
    """Check type and size against the workspace's upload policy (tenant.upload_policy)."""
    ext = file_extension(name)
    if ext not in policy.extensions:
        allowed = ", ".join(f".{e}" for e in policy.extensions)
        raise UploadRejected(f"{name}: file type not allowed in this workspace (allowed: {allowed})")
    if size > policy.max_file_bytes:
        raise UploadRejected(f"{name}: larger than the {policy.max_file_mb} MB limit")
    if size == 0:
        raise UploadRejected(f"{name}: file is empty")
    return ext


async def check_capacity(tenant, policy) -> int:
    """Remaining document slots; raises UploadRejected when the workspace is full."""
    used = await Document.objects.filter(tenant=tenant).acount()
    remaining = policy.max_documents - used
    if remaining <= 0:
        raise UploadRejected(
            f"This workspace has reached its limit of {policy.max_documents} "
            f"document{'' if policy.max_documents == 1 else 's'}. Remove one to upload another."
        )
    return remaining


async def ingest(tenant, name: str, data: bytes) -> Document:
    """Store a file and turn it into embedded chunks.

    Raises UploadRejected for invalid or duplicate files. Processing errors do
    not raise; the document is saved with status FAILED and an error message.
    """
    name = PurePath(name).name[:255] or "document"
    policy = tenant.upload_policy
    ext = validate_upload(name, len(data), policy)
    digest = hashlib.sha256(data).hexdigest()

    existing = await Document.objects.filter(tenant=tenant, sha256=digest).afirst()
    if existing:
        raise UploadRejected(f"{name}: already uploaded as “{existing.name}”")
    try:
        await check_capacity(tenant, policy)
    except UploadRejected as exc:
        raise UploadRejected(f"{name}: {exc}") from None

    document = Document(tenant=tenant, name=name, extension=ext, size=len(data), sha256=digest)
    # FileField.save writes the file and the row (sync storage API).
    await sync_to_async(document.file.save)(name, ContentFile(data), save=True)
    return await process(document, data)


class Cancelled(Exception):
    """The document was deleted while it was being processed."""


async def process(document: Document, data: bytes | None = None,
                  trigger: str = IndexingRun.Trigger.UPLOAD) -> Document:
    """(Re)build a document's chunks and embeddings from its stored file.

    Records an IndexingRun with timings. Stops early if the document is deleted
    meanwhile (checked between embedding batches).
    """
    document.status = Document.Status.PROCESSING
    document.error = ""
    config = await llm.aget_config()
    cfg = await conf.aget()
    started = time.monotonic()
    run = IndexingRun(
        tenant_id=document.tenant_id, document=document, document_name=document.name, trigger=trigger,
        extension=document.extension, size=document.size, embed_model=config.embed_model,
    )
    try:
        if data is None:
            data = await sync_to_async(_read_file)(document)
        text = await sync_to_async(extract_text)(data, document.extension)
        run.characters = len(text)
        run.extract_ms = int((time.monotonic() - started) * 1000)
        pieces = chunk_text(text, cfg.chunk_size, cfg.chunk_overlap)
        if not pieces:
            raise ExtractionError("No readable text found (scanned PDFs need OCR first)")

        embed_started = time.monotonic()
        vectors = []
        batch = cfg.embed_batch_size
        prefix = config.document_prefix
        for i in range(0, len(pieces), batch):
            if i and not await Document.objects.filter(pk=document.pk).aexists():
                raise Cancelled()
            inputs = [prefix + piece for piece in pieces[i : i + batch]]
            vectors.extend(await llm.embed(inputs))
        run.embed_ms = int((time.monotonic() - embed_started) * 1000)

        if not await Document.objects.filter(pk=document.pk).aexists():
            raise Cancelled()
        await Chunk.objects.filter(document=document).adelete()
        try:
            await Chunk.objects.abulk_create(
                Chunk(
                    tenant_id=document.tenant_id, document=document, index=i, text=piece,
                    embedding=Chunk.pack(vec), dimensions=len(vec),
                )
                for i, (piece, vec) in enumerate(zip(pieces, vectors))
            )
        except IntegrityError:  # deleted at the last moment
            raise Cancelled() from None
        document.status = Document.Status.READY
        document.chunk_count = len(pieces)
        document.embed_model = config.embed_model
        document.embed_signature = embed_signature(config)
        run.status, run.chunks = IndexingRun.Status.READY, len(pieces)
    except Cancelled:
        logger.info("Processing of document %s stopped: it was deleted", document.pk)
        run.document = None
        run.status, run.error = IndexingRun.Status.CANCELLED, "Document deleted while processing"
        run.total_ms = int((time.monotonic() - started) * 1000)
        await run.asave()
        return document
    except (ExtractionError, llm.LLMError, OSError) as exc:
        logger.warning("Processing failed for document %s: %s", document.pk, exc)
        await Chunk.objects.filter(document=document).adelete()
        document.status = Document.Status.FAILED
        document.error = str(exc)
        document.chunk_count = 0
        run.status, run.error = IndexingRun.Status.FAILED, str(exc)
    run.total_ms = int((time.monotonic() - started) * 1000)
    updated = await Document.objects.filter(pk=document.pk).aupdate(
        status=document.status, chunk_count=document.chunk_count, error=document.error,
        embed_model=document.embed_model, embed_signature=document.embed_signature, updated_at=timezone.now(),
    )
    if not updated:  # deleted during the final step; its chunks went with it
        run.document = None
        run.status, run.error, run.chunks = IndexingRun.Status.CANCELLED, "Document deleted while processing", 0
    await run.asave()
    logger.info("Indexed %s: %s, %s chunks in %.1fs (extract %.1fs, embed %.1fs)", document.name, run.status,
                run.chunks, run.total_ms / 1000, (run.extract_ms or 0) / 1000, (run.embed_ms or 0) / 1000)
    return document


def _read_file(document: Document) -> bytes:
    with document.file.open("rb") as f:
        return f.read()


async def delete_document(document: Document) -> None:
    """Delete a document; its chunks cascade and its file is removed (rag/signals.py)."""
    await document.adelete()


def _remove_empty_dirs(directory: Path) -> None:
    """Remove now-empty upload folders, stopping at the tenant's folder."""
    root = Path(settings.MEDIA_ROOT) / "rag"
    try:
        directory.relative_to(root)
    except ValueError:
        return
    while directory.parent != root and directory != root:
        try:
            directory.rmdir()  # only succeeds when empty
        except OSError:
            return
        directory = directory.parent


async def report(tenant) -> Report:
    ready = Q(status=Document.Status.READY)
    signature = embed_signature(await llm.aget_config())
    stats = await Document.objects.filter(tenant=tenant).aaggregate(
        documents=Count("id", filter=ready),
        stale=Count("id", filter=ready & ~Q(embed_signature=signature)),
        failed=Count("id", filter=Q(status=Document.Status.FAILED)),
        processing=Count("id", filter=Q(status=Document.Status.PROCESSING)),

        total_bytes=Sum("size", filter=Q(status=Document.Status.READY)),
    )
    # Count the chunks that actually exist, so the report can never drift from the index.
    chunks = await Chunk.objects.filter(tenant=tenant, document__status=Document.Status.READY).acount()
    return Report(
        documents=stats["documents"],
        chunks=chunks,
        failed=stats["failed"],
        processing=stats["processing"],
        total_bytes=stats["total_bytes"] or 0,
        stale=stats["stale"],
    )
