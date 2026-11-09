from array import array

from django.conf import settings
from django.db import models

from tenants.models import Tenant


def document_upload_path(instance, filename):
    # Files are grouped per tenant and never served publicly (no MEDIA url route).
    return f"rag/{instance.tenant.slug}/{instance.sha256[:2]}/{instance.sha256}/{filename}"


class Document(models.Model):
    """A file uploaded to a workspace's knowledge base."""

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to=document_upload_path, max_length=500)
    extension = models.CharField(max_length=16)
    size = models.PositiveBigIntegerField(help_text="Bytes")
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING)
    error = models.TextField(blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    embed_model = models.CharField(max_length=200, blank=True)
    embed_signature = models.CharField(
        max_length=300, blank=True,
        help_text="Embedding model + document prefix used; chunks are only searched when it matches the current settings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "sha256"], name="rag_unique_document_per_tenant"),
        ]

    def __str__(self):
        return self.name


class Chunk(models.Model):
    """A slice of a document's text plus its embedding vector, ready for retrieval."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="chunks")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    index = models.PositiveIntegerField()
    text = models.TextField()
    embedding = models.BinaryField(help_text="float32 vector")
    dimensions = models.PositiveIntegerField()

    class Meta:
        ordering = ["document_id", "index"]
        constraints = [
            models.UniqueConstraint(fields=["document", "index"], name="rag_unique_chunk_index"),
        ]
        indexes = [models.Index(fields=["tenant"])]

    def __str__(self):
        return f"{self.document} #{self.index}"

    @staticmethod
    def pack(vector):
        return array("f", vector).tobytes()

    def vector(self):
        values = array("f")
        values.frombytes(bytes(self.embedding))
        return values


def embed_signature(config) -> str:
    """Identifies how chunks are embedded; a change means documents must be reindexed."""
    return f"{config.embed_model}|{config.document_prefix}"


def current_embed_signature() -> str:
    """embed_signature() of the effective config. Sync contexts only."""
    from llm import get_config

    return embed_signature(get_config())


class FAQ(models.Model):
    """A curated question and answer for one workspace, answered without calling the model."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="faqs", verbose_name="workspace")
    question = models.CharField(max_length=500)
    alternate_questions = models.TextField(
        "Other phrasings", blank=True,
        help_text="Optional. One per line: other ways people ask this, e.g. 'How much vacation do I get?'. "
                  "A question matches directly when it uses no key words outside the FAQ's wording.",
    )
    answer = models.TextField(help_text="Shown as-is in the chat (Markdown supported).")
    enabled = models.BooleanField(default=True)

    # Embeddings of question + other phrasings (float32, concatenated), for matching.
    vectors = models.BinaryField(null=True, blank=True, editable=False)
    dimensions = models.PositiveIntegerField(null=True, blank=True, editable=False)
    embed_signature = models.CharField(max_length=400, blank=True, editable=False)

    hit_count = models.PositiveIntegerField("Times used", default=0, editable=False)
    last_hit_at = models.DateTimeField("Last used", null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant", "question"]
        verbose_name = "FAQ"
        verbose_name_plural = "FAQs"

    def __str__(self):
        return self.question

    def phrasings(self) -> list[str]:
        others = [line.strip() for line in (self.alternate_questions or "").splitlines() if line.strip()]
        return list(dict.fromkeys([self.question.strip(), *others]))

    def matrix(self):
        """Phrasing vectors as a (n, dims) float32 array, or None if not indexed."""
        import numpy as np

        if not self.vectors or not self.dimensions:
            return None
        return np.frombuffer(bytes(self.vectors), dtype=np.float32).reshape(-1, self.dimensions)


class IndexingRun(models.Model):
    """One processing of a document (upload, rebuild or reindex), for the Performance report.
    Kept when the document is deleted, so history and statistics stay intact."""

    class Trigger(models.TextChoices):
        UPLOAD = "upload", "Upload"
        REBUILD = "rebuild", "Rebuild"
        REINDEX = "reindex", "Reindex"

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"  # document deleted while processing

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="indexing_runs")
    document = models.ForeignKey(Document, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs")
    document_name = models.CharField(max_length=255)
    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.UPLOAD)
    status = models.CharField(max_length=16, choices=Status.choices)
    error = models.TextField(blank=True)
    extension = models.CharField(max_length=16, blank=True)
    size = models.PositiveBigIntegerField(default=0, help_text="Bytes")
    characters = models.PositiveIntegerField(null=True, blank=True, help_text="Extracted text length")
    chunks = models.PositiveIntegerField(default=0)
    extract_ms = models.PositiveIntegerField(null=True, blank=True, help_text="Reading the file and extracting text")
    embed_ms = models.PositiveIntegerField(null=True, blank=True, help_text="Splitting into chunks and embedding them")
    total_ms = models.PositiveIntegerField(null=True, blank=True)
    embed_model = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "-created_at"])]

    def __str__(self):
        return f"{self.document_name} ({self.get_trigger_display()}, {self.get_status_display()})"
