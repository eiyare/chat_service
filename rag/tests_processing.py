"""Per-document processing statistics (uploads, rebuilds, reindexing)."""
import shutil
import tempfile
from io import StringIO
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm
from chat.metrics import compute_processing
from rag import services
from rag.models import Chunk, Document, IndexingRun
from tenants.models import Tenant


async def fake_embed(texts):
    return [[1.0, 0.0, 0.5] for _ in texts]


@override_settings(RAG_CHUNK_SIZE=100, RAG_CHUNK_OVERLAP=10, RAG_EMBED_BATCH_SIZE=4, LLM_EMBED_MODEL="p/embed",
                   RAG_DOCUMENT_PREFIX="", RAG_QUERY_PREFIX="", RAG_MAX_DOCUMENTS=10, LLM_CONFIG_CACHE_SECONDS=0)
@mock.patch("llm.embed", fake_embed)
class ProcessingRunTests(TestCase):
    def setUp(self):
        llm.invalidate()
        self.media = tempfile.mkdtemp()
        o = override_settings(MEDIA_ROOT=self.media)
        o.enable()
        self.addCleanup(o.disable)
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.t = Tenant.objects.create(name="Math", slug="math")

    def upload(self, name="book.txt", words=60):
        body = " ".join(f"Theorem {i} states something useful." for i in range(words)).encode()
        self.client.post("/t/math/admin/documents/upload/", {"files": [SimpleUploadedFile(name, body)]})
        return Document.objects.get(name=name)

    def test_upload_records_timings_and_counts(self):
        doc = self.upload()
        run = IndexingRun.objects.get()
        self.assertEqual((run.trigger, run.status, run.document_id, run.document_name),
                         ("upload", "ready", doc.pk, "book.txt"))
        self.assertEqual((run.chunks, run.size, run.embed_model), (doc.chunk_count, doc.size, "p/embed"))
        self.assertGreater(run.characters, 1000)
        for field in ("extract_ms", "embed_ms", "total_ms"):
            self.assertIsNotNone(getattr(run, field), field)
        self.assertGreaterEqual(run.total_ms, run.extract_ms)

    def test_failed_and_rebuild_runs(self):
        with mock.patch("llm.embed", side_effect=llm.LLMError("Cannot reach the model provider")):
            doc = self.upload()
        failed = IndexingRun.objects.get()
        self.assertEqual((failed.status, failed.chunks, failed.error), ("failed", 0, "Cannot reach the model provider"))
        self.client.post(f"/t/math/admin/documents/{doc.pk}/rebuild/")
        rebuild = IndexingRun.objects.order_by("-id").first()
        self.assertEqual((rebuild.trigger, rebuild.status), ("rebuild", "ready"))

    def test_reindex_command_trigger(self):
        doc = self.upload()
        Document.objects.filter(pk=doc.pk).update(embed_signature="old|")
        call_command("reindex_documents", stdout=StringIO())
        self.assertEqual(IndexingRun.objects.order_by("-id").first().trigger, "reindex")

    def test_deleting_document_mid_processing_stops_it(self):
        doc = self.upload(words=5)
        calls = []

        async def embed_then_delete(texts):
            calls.append(len(texts))
            if len(calls) == 1:
                await Document.objects.filter(pk=doc.pk).adelete()
            return [[1.0, 0.0, 0.5] for _ in texts]

        with mock.patch("llm.embed", embed_then_delete):
            body = (" ".join(f"Lemma {i} holds in general." for i in range(80))).encode()
            async_to_sync(services.process)(doc, body, trigger="rebuild")
        self.assertEqual(len(calls), 1)  # stopped after the first batch instead of embedding the rest
        run = IndexingRun.objects.order_by("-id").first()
        self.assertEqual((run.status, run.document_id, run.chunks), ("cancelled", None, 0))
        self.assertEqual(Chunk.objects.count(), 0)

    def test_history_survives_document_deletion(self):
        doc = self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/t/math/admin/documents/{doc.pk}/delete/")
        run = IndexingRun.objects.get()
        self.assertIsNone(run.document_id)
        self.assertEqual(run.document_name, "book.txt")

    def test_report_and_documents_tab(self):
        self.upload("a.txt")
        self.upload("b.txt", words=20)
        page = self.client.get("/t/math/admin/performance/")
        self.assertContains(page, "Document processing")
        self.assertContains(page, "No questions in this period yet")  # section shows without questions
        self.assertContains(page, "a.txt")
        self.assertContains(page, "chunks/s")
        docs = self.client.get("/t/math/admin/")
        self.assertContains(docs, "Indexed in")

    def test_compute_processing(self):
        runs = [
            {"trigger": "upload", "status": "ready", "chunks": 100, "size": 2000, "extract_ms": 500,
             "embed_ms": 10000, "total_ms": 11000, "document_name": "a", "error": "", "characters": 9, "embed_model": "",
             "created_at": __import__("django.utils.timezone", fromlist=["now"]).now()},
            {"trigger": "rebuild", "status": "ready", "chunks": 50, "size": 1000, "extract_ms": 300,
             "embed_ms": 5000, "total_ms": 6000, "document_name": "b", "error": "", "characters": 9, "embed_model": "",
             "created_at": __import__("django.utils.timezone", fromlist=["now"]).now()},
            {"trigger": "upload", "status": "failed", "chunks": 0, "size": 10, "extract_ms": None,
             "embed_ms": None, "total_ms": 50, "document_name": "c", "error": "x", "characters": None, "embed_model": "",
             "created_at": __import__("django.utils.timezone", fromlist=["now"]).now()},
        ]
        p = compute_processing(runs)
        self.assertEqual((p["runs"], p["uploads"], p["failed"], p["chunks"], p["bytes"]), (3, 2, 1, 150, 3000))
        self.assertEqual((p["total_p50_ms"], p["total_max_ms"], p["extract_p50_ms"]), (6000, 11000, 300))
        self.assertAlmostEqual(p["chunks_per_second"], 10.0)
