import io
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm
from tenants.models import Tenant

from .chunking import chunk_text
from .extract import extract_text
from .models import Chunk, Document

EMBED_CALLS = []


async def fake_embed(texts):
    EMBED_CALLS.append(list(texts))
    return [[float(len(t)), 1.0, 0.5] for t in texts]


async def failing_embed(texts):
    raise llm.LLMError("Model not found: prov/embed")


class ChunkingTests(SimpleTestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_text("Hello   world.\n\n\n\nBye", 100, 10), ["Hello world.\n\nBye"])

    def test_empty_text(self):
        self.assertEqual(chunk_text("  \n\n ", 100, 10), [])

    def test_chunks_respect_size_and_overlap(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(200))
        chunks = chunk_text(text, 200, 40)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        # Consecutive chunks overlap, and every sentence survives.
        self.assertTrue(any(chunks[0][-20:] in chunks[1] for _ in [0]))
        for i in (0, 77, 199):
            self.assertTrue(any(f"Sentence number {i} is here." in c for c in chunks))

    def test_prefers_paragraph_boundaries(self):
        text = ("a" * 60 + "\n\n") * 5
        chunks = chunk_text(text, 100, 10)
        # Every chunk is made of whole paragraphs: no cut or overlap lands mid-paragraph.
        self.assertTrue(all(len(p) == 60 for c in chunks for p in c.split("\n\n")))

    def test_unbroken_text_still_terminates(self):
        chunks = chunk_text("x" * 1050, 100, 20)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertGreaterEqual(sum(len(c) for c in chunks), 1050)


class ExtractTests(SimpleTestCase):
    def test_text_and_bom(self):
        self.assertEqual(extract_text("﻿héllo".encode("utf-8"), "txt"), "héllo")

    def test_docx(self):
        from docx import Document as Docx

        doc = Docx()
        doc.add_paragraph("Quarterly revenue grew.")
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "Q1"
        table.rows[0].cells[1].text = "42"
        buf = io.BytesIO()
        doc.save(buf)
        text = extract_text(buf.getvalue(), "docx")
        self.assertIn("Quarterly revenue grew.", text)
        self.assertIn("Q1 | 42", text)


class MediaRootMixin:
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)


@override_settings(RAG_CHUNK_SIZE=100, RAG_CHUNK_OVERLAP=10, RAG_EMBED_BATCH_SIZE=4,
                   LLM_EMBED_MODEL="prov/embed", RAG_DOCUMENT_PREFIX="d: ", RAG_QUERY_PREFIX="q: ")
@mock.patch("llm.embed", fake_embed)
class AdminPageTests(MediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        EMBED_CALLS.clear()
        self.demo = Tenant.objects.create(name="Demo Co", slug="demo")
        self.other = Tenant.objects.create(name="Other", slug="other")

    def upload(self, *files, slug="demo"):
        return self.client.post(f"/t/{slug}/admin/documents/upload/", {"files": list(files)}, follow=True)

    def text_file(self, name="notes.txt", body=None):
        body = body or " ".join(f"Fact {i} about the product." for i in range(60))
        return SimpleUploadedFile(name, body.encode())

    def test_admin_page_renders_with_toggle(self):
        resp = self.client.get("/t/demo/admin/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Knowledge base")
        self.assertContains(resp, 'href="/t/demo/chat/"')
        self.assertContains(resp, "No documents yet")

    def test_admin_page_warns_without_embedding_model(self):
        with self.settings(LLM_EMBED_MODEL=""):
            resp = self.client.get("/t/demo/admin/")
        self.assertContains(resp, "No embedding model is enabled")
        self.assertNotContains(self.client.get("/t/demo/admin/"), "No embedding model is enabled")

    def test_chat_page_links_to_admin(self):
        resp = self.client.get("/t/demo/chat/")
        self.assertContains(resp, 'href="/t/demo/admin/"')

    def test_upload_creates_chunks_and_report(self):
        resp = self.upload(self.text_file())
        self.assertContains(resp, "chunks added")

        doc = Document.objects.get(tenant=self.demo)
        self.assertEqual(doc.status, Document.Status.READY)
        self.assertGreater(doc.chunk_count, 4)  # forces multiple embed batches
        self.assertEqual(Chunk.objects.filter(document=doc).count(), doc.chunk_count)
        self.assertTrue(all(len(texts) <= 4 for texts in EMBED_CALLS))
        # Document prefix is applied to what gets embedded, not to the stored text.
        self.assertTrue(all(t.startswith("d: ") for texts in EMBED_CALLS for t in texts))
        self.assertEqual((doc.embed_model, doc.embed_signature), ("prov/embed", "prov/embed|d: "))

        chunk = Chunk.objects.filter(document=doc).first()
        self.assertFalse(chunk.text.startswith("d: "))
        self.assertEqual(chunk.dimensions, 3)
        self.assertEqual(list(chunk.vector()), [float(len(chunk.text) + 3), 1.0, 0.5])

        self.assertEqual(resp.context["report"].documents, 1)
        self.assertEqual(resp.context["report"].chunks, doc.chunk_count)
        self.assertContains(resp, "notes.txt")

    def test_duplicate_rejected(self):
        self.upload(self.text_file())
        resp = self.upload(self.text_file(name="copy.txt"))
        self.assertContains(resp, "already uploaded")
        self.assertEqual(Document.objects.count(), 1)

    def test_same_file_allowed_in_another_tenant(self):
        self.upload(self.text_file())
        self.upload(self.text_file(), slug="other")
        self.assertEqual(Document.objects.count(), 2)

    def test_unsupported_type_rejected(self):
        resp = self.upload(SimpleUploadedFile("evil.exe", b"MZ..."))
        self.assertContains(resp, "file type not allowed in this workspace")
        self.assertEqual(Document.objects.count(), 0)

    @override_settings(RAG_MAX_UPLOAD_MB=0)
    def test_too_large_rejected(self):
        resp = self.upload(self.text_file())
        self.assertContains(resp, "larger than")
        self.assertEqual(Document.objects.count(), 0)

    def test_no_text_marks_failed(self):
        resp = self.upload(SimpleUploadedFile("blank.txt", b"   \n\n  "))
        doc = Document.objects.get()
        self.assertEqual(doc.status, Document.Status.FAILED)
        self.assertContains(resp, "No readable text")
        self.assertEqual(resp.context["report"].documents, 0)
        self.assertEqual(resp.context["report"].failed, 1)

    def test_embedding_failure_marks_failed(self):
        with mock.patch("llm.embed", failing_embed):
            self.upload(self.text_file())
        doc = Document.objects.get()
        self.assertEqual(doc.status, Document.Status.FAILED)
        self.assertIn("not found", doc.error)
        self.assertEqual(Chunk.objects.count(), 0)

    def test_report_and_list_are_tenant_scoped(self):
        self.upload(self.text_file(), slug="other")
        resp = self.client.get("/t/demo/admin/")
        self.assertEqual(resp.context["report"].documents, 0)
        self.assertEqual(resp.context["report"].chunks, 0)
        self.assertNotContains(resp, "notes.txt")

    def test_delete_removes_chunks_and_file(self):
        self.upload(self.text_file())
        doc = Document.objects.get()
        path = doc.file.path
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f"/t/demo/admin/documents/{doc.pk}/delete/", follow=True)
        self.assertContains(resp, "Removed notes.txt")
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(Chunk.objects.count(), 0)
        import os
        self.assertFalse(os.path.exists(path))

    def test_cannot_delete_other_tenants_document(self):
        self.upload(self.text_file(), slug="other")
        doc = Document.objects.get()
        resp = self.client.post(f"/t/demo/admin/documents/{doc.pk}/delete/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Document.objects.count(), 1)

    def test_unknown_tenant_404(self):
        self.assertEqual(self.client.get("/t/nope/admin/").status_code, 404)

    def test_delete_removes_empty_upload_folders(self):
        import os
        self.upload(self.text_file())
        doc = Document.objects.get()
        folder = os.path.dirname(doc.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/t/demo/admin/documents/{doc.pk}/delete/")
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isdir(os.path.join(self.media, "rag", "demo")))

    def test_stale_documents_reported_and_reindexed(self):
        from io import StringIO

        from django.core.management import call_command

        self.upload(self.text_file())
        with self.settings(LLM_EMBED_MODEL="prov/new-embed"):
            resp = self.client.get("/t/demo/admin/")
            self.assertEqual(resp.context["report"].stale, 1)
            self.assertContains(resp, "need reindexing")

            out = StringIO()
            call_command("reindex_documents", stdout=out)
            self.assertIn("Reindexed 1 document(s), 0 failed.", out.getvalue())
            doc = Document.objects.get()
            self.assertEqual(doc.embed_signature, "prov/new-embed|d: ")
            self.assertEqual(Chunk.objects.filter(document=doc).count(), doc.chunk_count)
            self.assertEqual(self.client.get("/t/demo/admin/").context["report"].stale, 0)

            out = StringIO()
            call_command("reindex_documents", stdout=out)
            self.assertIn("Nothing to reindex.", out.getvalue())


QUERY_VECTORS = {}


async def query_embed(texts):
    return [QUERY_VECTORS[t] for t in texts]


@override_settings(LLM_EMBED_MODEL="prov/embed", RAG_DOCUMENT_PREFIX="", RAG_QUERY_PREFIX="",
                   RAG_TOP_K=2, RAG_MIN_SCORE=0.5)
@mock.patch("llm.embed", query_embed)
class RetrievalTests(TestCase):
    def setUp(self):
        from .models import current_embed_signature

        self.t = Tenant.objects.create(name="T", slug="t")
        self.other = Tenant.objects.create(name="O", slug="o")
        sig = current_embed_signature()

        def doc(tenant, name, vectors, status=Document.Status.READY, signature=sig):
            d = Document.objects.create(tenant=tenant, name=name, extension="md", size=1, sha256=name,
                                        status=status, embed_signature=signature, chunk_count=len(vectors))
            for i, v in enumerate(vectors):
                Chunk.objects.create(tenant=tenant, document=d, index=i, text=f"{name}#{i}",
                                     embedding=Chunk.pack(v), dimensions=len(v))

        doc(self.t, "a", [[1, 0, 0], [0.9, 0.1, 0], [0.7, 0.7, 0]])
        doc(self.t, "b", [[0, 1, 0]])
        doc(self.t, "failed", [[1, 0, 0]], status=Document.Status.FAILED)
        doc(self.t, "stale", [[1, 0, 0]], signature="old|")
        doc(self.other, "foreign", [[1, 0, 0]])

    async def search(self, *queries, **kw):
        from .retrieval import search
        return await search(self.t, list(queries), **kw)

    async def test_top_k_best_first_with_scores(self):
        QUERY_VECTORS["x"] = [1, 0, 0]
        results = await self.search("x")
        self.assertEqual([p.text for p in results], ["a#0", "a#1"])
        self.assertAlmostEqual(results[0].score, 1.0, places=5)
        self.assertGreater(results[0].score, results[1].score)

    async def test_min_score_filters(self):
        QUERY_VECTORS["x"] = [0, 0, 1]
        self.assertEqual(await self.search("x"), [])

    async def test_excludes_other_tenants_failed_and_stale(self):
        QUERY_VECTORS["x"] = [1, 0, 0]
        results = await self.search("x", top_k=10)
        self.assertEqual({p.document for p in results}, {"a"})

    async def test_multiple_queries_take_best_score(self):
        QUERY_VECTORS["x"] = [0, 1, 0]
        QUERY_VECTORS["y"] = [1, 0, 0]
        results = await self.search("x", "y", top_k=10)
        self.assertEqual({p.text for p in results}, {"a#0", "a#1", "a#2", "b#0"})
        self.assertAlmostEqual(next(p for p in results if p.text == "b#0").score, 1.0, places=5)

    async def test_dimension_mismatch_ignored(self):
        QUERY_VECTORS["x"] = [1, 0]
        self.assertEqual(await self.search("x"), [])

    async def test_source_payload(self):
        QUERY_VECTORS["x"] = [1, 0, 0]
        source = (await self.search("x"))[0].as_source(1)
        self.assertEqual(set(source), {"n", "chunk_id", "document_id", "document", "index", "score"})


class LexicalTests(SimpleTestCase):
    def test_terms_drop_stopwords_and_stem(self):
        from .lexical import terms
        self.assertEqual(terms("What does the Growth plan cost?"), {"growth", "plan", "cost"})
        self.assertEqual(terms("Costs, offers and carried"), {"cost", "offer", "carry"})
        self.assertEqual(terms("What is it?"), set())

    def test_coverage(self):
        from .lexical import coverage
        cov, matched = coverage("Growth plan cost and CEO", ["The Growth plan costs 149 USD.", "Nothing here."])
        self.assertAlmostEqual(cov, 3 / 4)
        self.assertEqual(matched, [{"growth", "plan", "cost"}, set()])
        self.assertEqual(coverage("What is it?", ["x"])[0], None)


@override_settings(LLM_EMBED_MODEL="prov/embed", RAG_DOCUMENT_PREFIX="", RAG_QUERY_PREFIX="",
                   RAG_TOP_K=3, RAG_MIN_SCORE=0.5, RAG_MIN_TERM_OVERLAP=0.5)
@mock.patch("llm.embed", query_embed)
class TermOverlapGateTests(TestCase):
    """Embedding scores are deliberately high for everything; only the term check differs."""

    def setUp(self):
        from .models import current_embed_signature

        self.t = Tenant.objects.create(name="T", slug="t")
        texts = [
            "A quadratic equation contains a term in x squared.",
            "Employees get 25 days of annual leave.",
            "The Growth plan costs 149 USD.",
        ]
        doc = Document.objects.create(tenant=self.t, name="d", extension="md", size=1, sha256="d",
                                      status=Document.Status.READY, chunk_count=3,
                                      embed_signature=current_embed_signature())
        for i, text in enumerate(texts):
            Chunk.objects.create(tenant=self.t, document=doc, index=i, text=text,
                                 embedding=Chunk.pack([1, 0, 0]), dimensions=3)

    async def search(self, *queries):
        from .retrieval import search
        for q in queries:
            QUERY_VECTORS[q] = [1, 0, 0]  # every query "looks" relevant to every chunk
        return [p.text for p in await search(self.t, list(queries))]

    async def test_question_about_absent_subject_is_rejected(self):
        self.assertEqual(await self.search("What is Typhoid?"), [])

    async def test_matching_question_keeps_only_passages_with_its_terms(self):
        self.assertEqual(await self.search("What is a quadratic equation?"),
                         ["A quadratic equation contains a term in x squared."])

    async def test_multi_part_question_keeps_each_relevant_passage(self):
        found = await self.search("How many days of leave, and what does the Growth plan cost?")
        self.assertEqual(set(found), {"Employees get 25 days of annual leave.", "The Growth plan costs 149 USD."})

    async def test_mostly_uncovered_question_is_rejected(self):
        # Only "plan" of {plan, ceo, salary, headquarters} appears -> 25% < 50%.
        self.assertEqual(await self.search("Plan for the CEO salary and headquarters?"), [])

    async def test_follow_up_uses_best_covered_query(self):
        found = await self.search("And can I carry it over?", "How many days of leave?\nAnd can I carry it over?")
        self.assertEqual(found, ["Employees get 25 days of annual leave."])

    @override_settings(RAG_MIN_TERM_OVERLAP=0)
    async def test_gate_can_be_disabled(self):
        self.assertEqual(len(await self.search("What is Typhoid?")), 3)

    async def test_question_without_content_terms_skips_check(self):
        self.assertEqual(len(await self.search("What is it?")), 3)


@override_settings(LLM_EMBED_MODEL="prov/embed", RAG_DOCUMENT_PREFIX="", RAG_QUERY_PREFIX="",
                   RAG_TOP_K=3, RAG_MIN_SCORE=0.5, RAG_MIN_TERM_OVERLAP=0.5, RAG_KEYWORD_MIN_COVERAGE=0.75)
@mock.patch("llm.embed", query_embed)
class KeywordRouteTests(TestCase):
    """Names score poorly with embeddings; exact keyword matches must still be found."""

    def setUp(self):
        from .models import current_embed_signature

        self.t = Tenant.objects.create(name="T", slug="t")
        doc = Document.objects.create(tenant=self.t, name="profile", extension="md", size=1, sha256="p",
                                      status=Document.Status.READY, chunk_count=3,
                                      embed_signature=current_embed_signature())
        chunks = [
            ("Ei Consulting was founded by Chief Etinosa Iyare in Baltimore.", [0.3, 0.95, 0]),  # low similarity
            ("Leadership: Etinosa Iyare, CEO and Director of IT.", [0.3, 0.95, 0]),
            ("Our services include cloud migration and data analytics.", [1, 0, 0]),             # high similarity
        ]
        for i, (text, vec) in enumerate(chunks):
            Chunk.objects.create(tenant=self.t, document=doc, index=i, text=text,
                                 embedding=Chunk.pack(vec), dimensions=3)

    async def search(self, q, vec=(1, 0, 0)):
        from .retrieval import search
        QUERY_VECTORS[q] = list(vec)
        return [p.text for p in await search(self.t, [q])]

    async def test_name_found_by_keywords_despite_low_similarity(self):
        self.assertEqual(await self.search("Who is Chief Iyare?"),
                         ["Ei Consulting was founded by Chief Etinosa Iyare in Baltimore."])

    async def test_more_matching_terms_rank_first(self):
        found = await self.search("Is Iyare the CEO?", vec=(0.3, 0.95, 0))
        self.assertEqual(found[0], "Leadership: Etinosa Iyare, CEO and Director of IT.")

    async def test_partial_keyword_match_is_not_enough(self):
        # Only "iyare" of {iyare, salary, pension} appears.
        self.assertEqual(await self.search("Iyare salary pension"), [])

    async def test_absent_name_still_refused(self):
        self.assertEqual(await self.search("Who is Chief Okafor?"), [])

    @override_settings(RAG_KEYWORD_MIN_COVERAGE=0)
    async def test_keyword_route_can_be_disabled(self):
        self.assertEqual(await self.search("Who is Chief Iyare?"), [])


@override_settings(RAG_CHUNK_SIZE=100, RAG_CHUNK_OVERLAP=10, LLM_EMBED_MODEL="prov/embed",
                   RAG_DOCUMENT_PREFIX="", RAG_QUERY_PREFIX="",
                   RAG_MAX_UPLOAD_MB=5, RAG_MAX_DOCUMENTS=3, RAG_ALLOWED_EXTENSIONS=["txt", "pdf"])
@mock.patch("llm.embed", fake_embed)
class UploadLimitTests(MediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.t = Tenant.objects.create(name="Demo", slug="demo")

    def upload(self, *files):
        return self.client.post("/t/demo/admin/documents/upload/", {"files": list(files)}, follow=True)

    def f(self, name, body=b"Some useful text about leave policy."):
        return SimpleUploadedFile(name, body)

    def test_platform_defaults(self):
        policy = self.t.upload_policy
        self.assertEqual((policy.max_file_mb, policy.max_documents, policy.extensions), (5, 3, ("txt", "pdf")))

    def test_disallowed_types_rejected(self):
        for name in ("notes.md", "data.csv", "report.docx"):
            resp = self.upload(self.f(name))
            self.assertContains(resp, "file type not allowed")
        self.assertEqual(Document.objects.count(), 0)

    def test_size_limit(self):
        big = self.f("big.txt", b"a " * (5 * 1024 * 1024 // 2 + 10))
        self.assertContains(self.upload(big), "larger than the 5 MB limit")
        self.assertEqual(Document.objects.count(), 0)

    def test_document_count_limit(self):
        self.upload(self.f("a.txt", b"alpha text"), self.f("b.txt", b"beta text"))
        self.assertEqual(Document.objects.count(), 2)

        resp = self.upload(self.f("c.txt", b"gamma text"), self.f("d.txt", b"delta text"))
        self.assertContains(resp, "You selected 2 files, but this workspace has room for 1 more (limit 3)")
        self.assertEqual(Document.objects.count(), 2)  # whole batch refused

        self.upload(self.f("c.txt", b"gamma text"))
        resp = self.upload(self.f("e.txt", b"epsilon text"))
        self.assertContains(resp, "reached its limit of 3 documents")
        self.assertEqual(Document.objects.count(), 3)

        page = self.client.get("/t/demo/admin/")
        self.assertContains(page, "Document limit reached")
        self.assertContains(page, "3 of 3 documents")

        # Removing one frees a slot.
        doc = Document.objects.first()
        self.client.post(f"/t/demo/admin/documents/{doc.pk}/delete/")
        self.assertEqual(Document.objects.count(), 2)
        self.upload(self.f("e.txt", b"epsilon text"))
        self.assertEqual(Document.objects.count(), 3)

    def test_failed_documents_count_toward_limit(self):
        self.t.max_documents = 1
        self.t.save()
        self.upload(self.f("blank.txt", b"   "))
        self.assertEqual(Document.objects.get().status, Document.Status.FAILED)
        self.assertContains(self.upload(self.f("a.txt", b"alpha")), "reached its limit of 1 document.")

    def test_per_tenant_overrides(self):
        self.t.max_file_mb = 1
        self.t.max_documents = 10
        self.t.allowed_file_types = "md, .TXT"
        self.t.full_clean()
        self.t.save()
        policy = Tenant.objects.get(pk=self.t.pk).upload_policy
        self.assertEqual((policy.max_file_mb, policy.max_documents, policy.extensions), (1, 10, ("md", "txt")))

        self.assertContains(self.upload(self.f("notes.md", b"markdown text")), "chunk")
        self.assertContains(self.upload(self.f("x.pdf", b"%PDF")), "file type not allowed")
        big = self.f("big.txt", b"a" * (1024 * 1024 + 1))
        self.assertContains(self.upload(big), "larger than the 1 MB limit")

        page = self.client.get("/t/demo/admin/")
        self.assertContains(page, "MD, TXT · up to 1 MB each")
        self.assertContains(page, 'accept=".md,.txt"')

        # Other workspaces keep the defaults.
        other = Tenant.objects.create(name="Other", slug="other")
        self.assertEqual(other.upload_policy.extensions, ("txt", "pdf"))

    def test_unsupported_override_rejected(self):
        from django.core.exceptions import ValidationError
        self.t.allowed_file_types = "txt,exe"
        with self.assertRaisesMessage(ValidationError, "Not supported: exe"):
            self.t.full_clean()

    def test_tenant_admin_form(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_superuser("root", "r@example.com", "pw"))
        page = self.client.get(f"/admin/tenants/tenant/{self.t.pk}/change/")
        self.assertContains(page, "Knowledge base limits")
        self.assertContains(page, "Platform default: 5 MB")
        resp = self.client.post(f"/admin/tenants/tenant/{self.t.pk}/change/", {
            "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
            "memberships-MIN_NUM_FORMS": "0", "settings_override-TOTAL_FORMS": "0", "settings_override-INITIAL_FORMS": "0", "settings_override-MIN_NUM_FORMS": "0", "settings_override-MAX_NUM_FORMS": "1", "memberships-MAX_NUM_FORMS": "1000", "chatbot_name": "Tosin",
            "name": "Demo", "slug": "demo", "is_active": "on",
            "max_file_mb": "2", "max_documents": "", "allowed_file_types": "pdf",
        })
        self.assertEqual(resp.status_code, 302)
        policy = Tenant.objects.get(pk=self.t.pk).upload_policy
        self.assertEqual((policy.max_file_mb, policy.max_documents, policy.extensions), (2, 3, ("pdf",)))
        listing = self.client.get("/admin/tenants/tenant/")
        self.assertContains(listing, "0 / 3")
        self.assertContains(listing, "2 MB")


@override_settings(RAG_CHUNK_SIZE=100, RAG_CHUNK_OVERLAP=10, RAG_EMBED_BATCH_SIZE=4,
                   LLM_EMBED_MODEL="prov/embed", RAG_DOCUMENT_PREFIX="", RAG_QUERY_PREFIX="",
                   RAG_MAX_DOCUMENTS=10)
@mock.patch("llm.embed", fake_embed)
class DocumentChunkManagementTests(MediaRootMixin, TestCase):
    """Documents are managed; chunks are generated, read-only, and rebuilt from their document."""

    def setUp(self):
        super().setUp()
        import llm
        llm.invalidate()
        from django.contrib.auth import get_user_model
        self.t = Tenant.objects.create(name="Demo", slug="demo")
        self.admin = get_user_model().objects.create_superuser("root", "r@example.com", "pw")

    def add(self, name="notes.txt"):
        body = " ".join(f"Fact {i} about {name}." for i in range(40)).encode()
        self.client.post("/t/demo/admin/documents/upload/", {"files": [SimpleUploadedFile(name, body)]})
        return Document.objects.get(name=name)

    def report(self):
        from asgiref.sync import async_to_sync
        from .services import report
        return async_to_sync(report)(self.t)

    def test_django_admin_delete_removes_chunks_file_and_updates_counts(self):
        import os
        a, b = self.add("a.txt"), self.add("b.txt")
        paths = [a.file.path, b.file.path]
        before = self.report()
        self.assertEqual(before.documents, 2)
        self.client.force_login(self.admin)

        with self.captureOnCommitCallbacks(execute=True):  # single delete
            self.client.post(f"/admin/rag/document/{a.pk}/delete/", {"post": "yes"})
        self.assertFalse(os.path.exists(paths[0]))
        self.assertEqual(Chunk.objects.filter(document_id=a.pk).count(), 0)

        b2 = self.add("c.txt")
        with self.captureOnCommitCallbacks(execute=True):  # bulk delete
            self.client.post("/admin/rag/document/", {"action": "delete_selected", "index": 0, "post": "yes",
                                                      "_selected_action": [b.pk, b2.pk]})
        self.assertFalse(os.path.exists(paths[1]))
        self.assertEqual((Document.objects.count(), Chunk.objects.count()), (0, 0))
        after = self.report()
        self.assertEqual((after.documents, after.chunks), (0, 0))

    def test_deleting_a_workspace_removes_its_files(self):
        import os
        path = self.add().file.path
        with self.captureOnCommitCallbacks(execute=True):
            self.t.delete()
        self.assertFalse(os.path.exists(path))

    def test_chunks_are_read_only_in_admin(self):
        doc = self.add()
        chunk = doc.chunks.first()
        self.client.force_login(self.admin)
        listing = self.client.get("/admin/rag/chunk/")
        self.assertContains(listing, "can&#x27;t be edited or deleted here")
        self.assertNotContains(listing, "Delete selected")
        self.assertEqual(self.client.post(f"/admin/rag/chunk/{chunk.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get("/admin/rag/chunk/add/").status_code, 403)
        page = self.client.get(f"/admin/rag/chunk/{chunk.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="_save"')
        self.assertTrue(Chunk.objects.filter(pk=chunk.pk).exists())

    def test_rebuild_from_chunks_and_documents_actions(self):
        doc = self.add()
        n = doc.chunk_count
        Chunk.objects.filter(document=doc).order_by("index").last().delete()  # simulate a lost chunk
        self.assertEqual(self.report().chunks, n - 1)                         # report shows the real count

        self.client.force_login(self.admin)
        chunk = doc.chunks.first()
        resp = self.client.post("/admin/rag/chunk/", {"action": "rebuild_documents", "index": 0,
                                                      "_selected_action": [chunk.pk]}, follow=True)
        self.assertContains(resp, "Rebuilt chunks for 1 document")
        self.assertEqual(Chunk.objects.filter(document=doc).count(), n)
        self.assertEqual(self.report().chunks, n)

        resp = self.client.post("/admin/rag/document/", {"action": "rebuild_chunks", "index": 0,
                                                         "_selected_action": [doc.pk]}, follow=True)
        self.assertContains(resp, "Rebuilt chunks for 1 document")

    def test_document_admin_columns_and_no_add(self):
        doc = self.add()
        self.client.force_login(self.admin)
        listing = self.client.get("/admin/rag/document/")
        self.assertContains(listing, "Upload documents from each workspace")
        self.assertContains(listing, "Current")
        self.assertContains(listing, f"?document__id__exact={doc.pk}")
        self.assertEqual(self.client.get("/admin/rag/document/add/").status_code, 403)
        with self.settings(LLM_EMBED_MODEL="prov/other"):
            self.assertContains(self.client.get("/admin/rag/document/"), "Needs rebuild")
            self.assertContains(self.client.get("/admin/rag/document/?index=rebuild"), "notes.txt")

    def test_workspace_rebuild_button_for_failed_and_outdated(self):
        ok = self.add()
        page = self.client.get("/t/demo/admin/")
        self.assertNotContains(page, "Rebuild notes.txt")

        with mock.patch("llm.embed", side_effect=__import__("llm").LLMError("Cannot reach the model provider")):
            failed = self.add("down.txt")
        self.assertEqual(failed.status, Document.Status.FAILED)
        page = self.client.get("/t/demo/admin/")
        self.assertContains(page, "Rebuild down.txt")

        resp = self.client.post(f"/t/demo/admin/documents/{failed.pk}/rebuild/", follow=True)
        self.assertContains(resp, "down.txt: rebuilt")
        self.assertEqual(Document.objects.get(pk=failed.pk).status, Document.Status.READY)

        with self.settings(LLM_EMBED_MODEL="prov/other"):
            page = self.client.get("/t/demo/admin/")
            self.assertContains(page, "Needs rebuild")
            self.assertContains(page, "Rebuild notes.txt")

        other = Tenant.objects.create(name="Other", slug="other")
        self.assertEqual(self.client.post(f"/t/other/admin/documents/{ok.pk}/rebuild/").status_code, 404)
