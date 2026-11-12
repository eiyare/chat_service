"""Embedding test (manage.py eval_embeddings): decisions match live search, nothing is saved."""
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError

from llm.models import LLMModel, Provider
from rag import embedding_eval as ev
from rag.models import Chunk, Document, embed_signature
from rag.retrieval import search
from saas_chat.testing import TestCase
from tenants.models import Tenant

LEAVE, PRICING, OTHER = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]
CALLS = []


def vector_for(text):
    t = text.lower()
    if "leave" in t or "holiday" in t:
        return LEAVE
    if "price" in t or "cost" in t or "plan" in t:
        return PRICING
    return OTHER


async def fake_embed(texts, purpose="document", config=None):
    CALLS.append((config.embed_model if config else None, purpose, list(texts)))
    return [vector_for(t) for t in texts]


CASES = {
    "workspaces": {
        "acme": {
            "answerable": [
                {"q": "How much annual leave do I get?", "expect": ["25 days"]},
                {"q": "What does the Growth plan cost?", "expect": ["149"]},
                {"q": "Is there a dental plan?", "expect": ["dental"]},
                {"q": "How many holidays?", "expect": ["25 days"]},  # meaning only: no shared key word
            ],
            "off_topic": ["What is the capital of France?"],
        },
        "missing-workspace": {"answerable": [{"q": "x"}]},
    },
    "faq_pairs": [
        {"faq": "How much annual leave do I get?", "asked": "How much annual leave do employees get?", "expect": "same"},
        {"faq": "How much annual leave do I get?", "asked": "What does the plan cost?", "expect": "unrelated"},
    ],
}


@mock.patch("llm.embed", fake_embed)
class EmbeddingEvalTests(TestCase):
    def setUp(self):
        CALLS.clear()
        self.provider = Provider.objects.create(name="ollama", provider_type="ollama", endpoint="http://gpu:11434")
        self.active = LLMModel.objects.create(provider=self.provider, name="nomic-embed-text", kind="embedding",
                                              query_prefix="q: ", document_prefix="d: ", min_score=0.5, strong_score=0.9,
                                              faq_min_score=0.8)
        self.other = LLMModel.objects.create(provider=self.provider, name="other-embed", kind="embedding",
                                             enabled=False, min_score=0.9)
        self.acme = Tenant.objects.create(name="Acme", slug="acme")
        config = ev.candidate_config()
        self.doc = Document.objects.create(
            tenant=self.acme, name="handbook.txt", extension="txt", size=1, sha256="x",
            status=Document.Status.READY, chunk_count=2, embed_signature=embed_signature(config),
        )
        for i, text in enumerate(["Employees get 25 days of annual leave.", "The Growth plan costs 149 USD."]):
            Chunk.objects.create(tenant=self.acme, document=self.doc, index=i, text=text,
                                 embedding=Chunk.pack(vector_for(text)), dimensions=3)

    def run_eval(self, config):
        return async_to_sync(ev.run)(CASES, config, {"acme": self.acme})

    def test_candidate_config_is_in_memory_only(self):
        config = ev.candidate_config("ollama/other-embed", {"min_score": 0.4, "query_params": {"k": 1}})
        self.assertEqual(config.embed_model, "ollama/other-embed")
        self.assertEqual(config.doc_cutoff, 0.4)
        self.assertEqual(config.embed_query_params, {"k": 1})
        self.active.refresh_from_db()
        self.other.refresh_from_db()
        self.assertTrue(self.active.enabled)
        self.assertFalse(self.other.enabled)
        self.assertEqual(self.other.min_score, 0.9)
        self.assertEqual(self.other.query_params, {})

    def test_unknown_model_and_bad_override(self):
        with self.assertRaisesMessage(ev.EvalError, "No embedding model"):
            ev.candidate_config("ollama/nope")
        with self.assertRaisesMessage(ev.EvalError, "Cannot override"):
            ev.candidate_config(None, {"enabled": True})

    def test_outcomes_with_stored_vectors(self):
        report = self.run_eval(ev.candidate_config())
        self.assertTrue(report["workspaces"]["acme"]["reused_stored_vectors"])
        self.assertEqual(report["skipped"], ["missing-workspace"])
        self.assertFalse([c for c in CALLS if c[1] == "document"])  # documents not re-embedded
        outcome = {q["question"]: q["outcome"] for q in report["questions"]}
        self.assertEqual(outcome["How much annual leave do I get?"], "found")
        self.assertEqual(outcome["What does the Growth plan cost?"], "found")
        self.assertEqual(outcome["Is there a dental plan?"], "no_right_passage")  # case matches no passage
        self.assertEqual(outcome["What is the capital of France?"], "refused")
        s = report["summary"]
        self.assertEqual(outcome["How many holidays?"], "found")  # strong match skips the key-word check
        self.assertEqual((s["answerable"], s["found"], s["off_topic_refused"], s["bad_cases"]), (3, 3, 1, 1))
        self.assertEqual([p["ok"] for p in report["faq_pairs"]], [True, True])
        self.assertTrue(all(c[0] == "nomic-embed-text" or c[0] == "ollama/nomic-embed-text" for c in CALLS))

    def test_decisions_match_live_search(self):
        config = ev.candidate_config()
        index = async_to_sync(ev.build_index)(self.acme, config, None)
        from siteconfig import conf
        cfg = conf.get(self.acme)
        for question in ["How much annual leave do I get?", "What does the Growth plan cost?",
                         "What is the capital of France?", "annual leave plan cost"]:
            qv = ev._unit([vector_for(question)])[0]
            chosen, _ = ev.decide(question, qv, index, config, cfg)
            live = async_to_sync(search)(self.acme, [question], query_vectors=[vector_for(question)])
            self.assertEqual([p.text for p in chosen], [p.text for p in live], question)

    def test_other_model_embeds_documents_in_memory(self):
        with tempfile.TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            self.doc.file.save("handbook.txt", ContentFile(b"Employees get 25 days of annual leave."), save=True)
            before = Chunk.objects.count()
            report = self.run_eval(ev.candidate_config("ollama/other-embed", {"min_score": 0.5}))
        self.assertFalse(report["workspaces"]["acme"]["reused_stored_vectors"])
        self.assertEqual(report["workspaces"]["acme"]["embedded_texts"], 1)
        self.assertEqual(Chunk.objects.count(), before)  # nothing written
        self.assertIn(("ollama/other-embed", "document", ["Employees get 25 days of annual leave."]),
                      [(c[0], c[1], c[2]) for c in CALLS])

    def test_command_saves_and_compares(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.json"
            cases.write_text(json.dumps(CASES))
            with mock.patch("rag.management.commands.eval_embeddings.RESULTS_DIR", Path(tmp) / "results"):
                out = io.StringIO()
                call_command("eval_embeddings", "--cases", str(cases), "--no-color", stdout=out)
                self.assertIn("SCORE", out.getvalue())
                self.assertEqual(len(list((Path(tmp) / "results").glob("*.json"))), 1)
                out = io.StringIO()
                call_command("eval_embeddings", "--cases", str(cases), "--compare", "latest", "--no-save",
                             "--set", "min_score=1.01", "--no-color", stdout=out)
        text = out.getvalue()
        self.assertIn("Comparison", text)
        self.assertIn("unsaved overrides", text)
        self.assertIn("WORSE  found -> refused", text)
        self.assertIn("How many holidays?", text)

    def test_command_rejects_bad_input(self):
        with self.assertRaises(CommandError):
            call_command("eval_embeddings", "--model", "ollama/nope", stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("eval_embeddings", "--set", "min_score", stdout=io.StringIO())
