"""Admin-managed providers (connections) and models overriding the environment."""
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from saas_chat.testing import TestCase  # signed in by default

import llm
from llm import crypto
from llm.models import LLMModel, Provider

ENV = dict(
    LLM_CHAT_MODELS=["ollama_chat/env-a", "ollama_chat/env-b"],
    LLM_DEFAULT_CHAT_MODEL="ollama_chat/env-b",
    LLM_EMBED_MODEL="ollama/env-embed",
    RAG_QUERY_PREFIX="eq: ",
    RAG_DOCUMENT_PREFIX="ed: ",
    LLM_PROVIDERS={
        "ollama": {"api_base": "http://env-ollama:11434"},
        "ollama_chat": {"api_base": "http://env-ollama:11434", "num_ctx": 4096},
        "openai": {"api_key": "sk-env"},
        "openai_compatible": {"litellm_provider": "openai"},
    },
    LLM_MODEL_PARAMS={"ollama_chat/env-a": {"num_predict": 10}},
    LLM_CONFIG_CACHE_SECONDS=30,
    LLM_ENCRYPTION_KEY="",
    LLM_TIMEOUT=60.0,
    LLM_NUM_RETRIES=0,
)


def bedrock(**kw):
    p = Provider(name=kw.pop("name", "bedrock"), provider_type="bedrock", region=kw.pop("region", "us-east-1"), **kw)
    p.key = "AKIAEXAMPLEEXAMPLE12"
    p.secret = "very-secret"
    p.save()
    return p


class Base(TestCase):
    def setUp(self):
        llm.invalidate()
        self.addCleanup(llm.invalidate)


@override_settings(**ENV)
class EffectiveConfigTests(Base):
    def test_no_admin_rows_uses_environment(self):
        config = llm.get_config()
        self.assertEqual(config.chat_models, ("ollama_chat/env-a", "ollama_chat/env-b"))
        self.assertEqual(config.default_chat_model, "ollama_chat/env-b")
        self.assertEqual((config.embed_model, config.query_prefix), ("ollama/env-embed", "eq: "))
        self.assertEqual(config.sources, {"chat_models": "environment", "embed_model": "environment"})

    def test_provider_model_combination_routes_with_standard_fields(self):
        provider = bedrock(options={"max_retries": 2})
        LLMModel.objects.create(provider=provider, name="google.gemma-3-4b-it", label="Gemma", is_default=True,
                                params={"max_tokens": 100})
        config = llm.get_config()
        self.assertEqual(config.chat_models, ("bedrock/google.gemma-3-4b-it",))
        self.assertEqual(config.label("bedrock/google.gemma-3-4b-it"), "Gemma")
        model, kwargs = llm.resolve("bedrock/google.gemma-3-4b-it", config)
        self.assertEqual(model, "bedrock/google.gemma-3-4b-it")
        self.assertEqual(kwargs, {
            "timeout": 60.0, "aws_access_key_id": "AKIAEXAMPLEEXAMPLE12",
            "aws_secret_access_key": "very-secret", "aws_region_name": "us-east-1",
            "max_retries": 2, "max_tokens": 100,
        })

    def test_one_ollama_connection_serves_chat_and_embeddings(self):
        ollama = Provider.objects.create(name="ollama", provider_type="ollama", endpoint="http://gpu:11434")
        LLMModel.objects.create(provider=ollama, name="qwen2.5:3b")
        LLMModel.objects.create(provider=ollama, name="nomic-embed-text", kind="embedding",
                                query_prefix="q: ", document_prefix="d: ")
        config = llm.get_config()
        chat_model, chat_kwargs = llm.resolve("ollama/qwen2.5:3b", config)
        embed_model, embed_kwargs = llm.resolve(config.embed_model, config)
        self.assertEqual((chat_model, embed_model), ("ollama_chat/qwen2.5:3b", "ollama/nomic-embed-text"))
        self.assertEqual(chat_kwargs["api_base"], "http://gpu:11434")       # admin wins
        self.assertEqual(chat_kwargs["num_ctx"], 4096)                     # env fallback kept
        self.assertEqual((config.query_prefix, config.document_prefix), ("q: ", "d: "))

    def test_blank_fields_fall_back_to_environment(self):
        provider = Provider.objects.create(name="openai", provider_type="openai")  # key from env
        self.assertEqual(provider.missing_required(), [])
        LLMModel.objects.create(provider=provider, name="gpt-x")
        self.assertEqual(llm.resolve("openai/gpt-x", llm.get_config())[1]["api_key"], "sk-env")

    def test_openai_compatible_never_inherits_openai_key(self):
        provider = Provider.objects.create(name="lmstudio", provider_type="openai_compatible",
                                           endpoint="http://localhost:1234/v1")
        LLMModel.objects.create(provider=provider, name="local")
        model, kwargs = llm.resolve("lmstudio/local", llm.get_config())
        self.assertEqual(model, "openai/local")
        self.assertNotIn("api_key", kwargs)

    def test_disabled_or_incomplete_providers_hide_their_models(self):
        off = Provider.objects.create(name="ollama", provider_type="ollama", enabled=False)
        LLMModel.objects.create(provider=off, name="m1")
        incomplete = Provider(name="bedrock", provider_type="bedrock")
        incomplete.key = "AKIAEXAMPLEEXAMPLE12"
        incomplete.save()
        LLMModel.objects.create(provider=incomplete, name="m2")
        self.assertEqual(incomplete.missing_required(), ["Secret", "Region"])
        self.assertEqual(llm.get_config().chat_models, ())

    def test_changes_apply_without_restart(self):
        self.assertEqual(len(llm.get_config().chat_models), 2)
        LLMModel.objects.create(provider=bedrock(), name="m")
        self.assertEqual(llm.get_config().chat_models, ("bedrock/m",))

    def test_credentials_encrypted_at_rest(self):
        provider = bedrock()
        row = Provider.objects.get(pk=provider.pk)
        self.assertNotIn("very-secret", row.secret_encrypted)
        self.assertNotIn("AKIAEXAMPLE", row.key_encrypted)
        self.assertEqual((row.key, row.secret), ("AKIAEXAMPLEEXAMPLE12", "very-secret"))


@override_settings(**ENV)
class ValidationTests(Base):
    def test_required_fields_per_provider(self):
        with self.assertRaisesMessage(ValidationError, "AWS Bedrock requires: Key, Secret, Region"):
            Provider(name="b", provider_type="bedrock").full_clean()
        with self.assertRaisesMessage(ValidationError, "Azure OpenAI requires: Key, Endpoint, API version"):
            Provider(name="a", provider_type="azure").full_clean()
        Provider(name="o", provider_type="ollama").full_clean()  # nothing required

    def test_misplaced_values_rejected(self):
        p = Provider(name="b", provider_type="ollama", endpoint="AKIAEXAMPLEEXAMPLE12")
        with self.assertRaisesMessage(ValidationError, "looks like a credential"):
            p.full_clean()
        p = Provider(name="b", provider_type="ollama", endpoint="localhost:11434")
        with self.assertRaisesMessage(ValidationError, "http://"):
            p.full_clean()
        p = Provider(name="b", provider_type="ollama", options={"api_key": "x"})
        with self.assertRaisesMessage(ValidationError, "use Key / Secret"):
            p.full_clean()

    def test_embedding_kind_needs_supporting_provider(self):
        anthropic = Provider.objects.create(name="anthropic", provider_type="anthropic")
        with self.assertRaisesMessage(ValidationError, "has no embedding models"):
            LLMModel(provider=anthropic, name="x", kind="embedding").full_clean()


class CryptoTests(TestCase):
    def test_roundtrip_and_wrong_key(self):
        token = crypto.encrypt("secret")
        self.assertEqual(crypto.decrypt(token), "secret")
        with self.settings(LLM_ENCRYPTION_KEY="A" * 43 + "="):
            self.assertEqual(crypto.decrypt(token), "")


@override_settings(**ENV)
class AdminTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("root", "root@example.com", "pw"))

    def post_provider(self, url, **fields):
        data = {"name": "bedrock", "provider_type": "bedrock", "enabled": "on", "key": "", "secret": "",
                "endpoint": "", "region": "", "api_version": "", "project": "", "options": "{}", **fields}
        return self.client.post(url, data)

    def test_provider_form_standard_fields_write_only(self):
        resp = self.post_provider("/admin/llm/provider/add/", key="AKIAEXAMPLEEXAMPLE12",
                                  secret="very-secret", region="us-east-1")
        self.assertEqual(resp.status_code, 302)
        provider = Provider.objects.get()
        self.assertEqual((provider.key, provider.secret, provider.region),
                         ("AKIAEXAMPLEEXAMPLE12", "very-secret", "us-east-1"))
        for url in (f"/admin/llm/provider/{provider.pk}/change/", "/admin/llm/provider/"):
            page = self.client.get(url).content.decode()
            self.assertNotIn("very-secret", page)
            self.assertNotIn("AKIAEXAMPLEEXAMPLE12", page)
        self.assertContains(self.client.get("/admin/llm/provider/"), "Ready")

        url = f"/admin/llm/provider/{provider.pk}/change/"
        self.post_provider(url, region="us-east-1")  # blanks keep stored credentials
        self.assertEqual(Provider.objects.get().secret, "very-secret")
        resp = self.post_provider(url, region="us-east-1", clear_secret="on")  # required -> refused
        self.assertContains(resp, "requires: Secret")

    def test_missing_required_fields_reported(self):
        resp = self.post_provider("/admin/llm/provider/add/", key="AKIAEXAMPLEEXAMPLE12")
        self.assertContains(resp, "AWS Bedrock requires: Secret, Region")
        self.assertFalse(Provider.objects.exists())

    def test_fields_unused_by_type_are_dropped(self):
        self.post_provider("/admin/llm/provider/add/", name="ollama", provider_type="ollama",
                           region="us-east-1", project="p", secret="s", endpoint="http://x:11434")
        p = Provider.objects.get()
        self.assertEqual((p.region, p.project, p.has_value("secret"), p.endpoint), ("", "", False, "http://x:11434"))

    def test_form_page_carries_schema_and_script(self):
        page = self.client.get("/admin/llm/provider/add/").content.decode()
        self.assertIn("data-schema", page)
        self.assertIn("llm/provider_form.js", page)

    def test_model_form_and_changelist(self):
        provider = bedrock()
        resp = self.client.post("/admin/llm/llmmodel/add/", {
            "provider": provider.pk, "name": "google.gemma-3-4b-it", "kind": "chat", "label": "Gemma",
            "enabled": "on", "is_default": "on", "sort_order": 0, "params": "{}",
            "query_prefix": "", "document_prefix": "",
        })
        self.assertEqual(resp.status_code, 302)
        listing = self.client.get("/admin/llm/llmmodel/")
        self.assertContains(listing, "bedrock/google.gemma-3-4b-it")
        self.assertContains(listing, "Available")
        self.assertContains(listing, "In use now")

    def test_warnings_for_hidden_models(self):
        off = Provider.objects.create(name="ollama", provider_type="ollama", enabled=False)
        LLMModel.objects.create(provider=off, name="m1")
        incomplete = Provider(name="bedrock", provider_type="bedrock")
        incomplete.save()
        LLMModel.objects.create(provider=incomplete, name="m2")
        for url in ("/admin/llm/llmmodel/", "/admin/llm/provider/"):
            resp = self.client.get(url)
            self.assertContains(resp, "Provider &#x27;ollama&#x27; is disabled, hiding: ollama/m1")
            self.assertContains(resp, "is missing Key, Secret, Region, hiding: bedrock/m2")
            self.assertContains(resp, "No chat model is available")

    def test_test_action_uses_provider_connection(self):
        model = LLMModel.objects.create(provider=bedrock(), name="google.gemma-3-4b-it")
        fake = mock.AsyncMock(return_value=mock.Mock(choices=[mock.Mock(message=mock.Mock(content="OK"))]))
        with mock.patch("litellm.acompletion", fake):
            resp = self.client.post("/admin/llm/llmmodel/", {
                "action": "test_models", "index": 0, "_selected_action": [model.pk]}, follow=True)
        self.assertContains(resp, "Test of bedrock/google.gemma-3-4b-it: OK")
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["model"], "bedrock/google.gemma-3-4b-it")
        self.assertEqual((kwargs["aws_secret_access_key"], kwargs["aws_region_name"]), ("very-secret", "us-east-1"))

    def test_embedding_change_warns_to_reindex(self):
        ollama = Provider.objects.create(name="ollama", provider_type="ollama")
        resp = self.client.post("/admin/llm/llmmodel/add/", {
            "provider": ollama.pk, "name": "nomic-embed-text", "kind": "embedding", "label": "",
            "enabled": "on", "sort_order": 0, "params": "{}", "query_prefix": "", "document_prefix": "",
        }, follow=True)
        self.assertContains(resp, "must be reindexed")


@override_settings(**ENV)
class ImportCommandTests(Base):
    def test_import_creates_provider_model_rows_without_keys(self):
        call_command("import_llm_settings", stdout=StringIO())
        ollama = Provider.objects.get()
        self.assertEqual((ollama.name, ollama.provider_type, ollama.endpoint), ("ollama", "ollama", "http://env-ollama:11434"))
        self.assertEqual(ollama.options, {"num_ctx": 4096})
        self.assertEqual(
            sorted((m.identifier, m.kind, m.is_default) for m in LLMModel.objects.select_related("provider")),
            [("ollama/env-a", "chat", False), ("ollama/env-b", "chat", True), ("ollama/env-embed", "embedding", False)],
        )
        self.assertEqual(LLMModel.objects.get(name="env-a").params, {"num_predict": 10})
        call_command("import_llm_settings", stdout=StringIO())  # idempotent
        self.assertEqual(LLMModel.objects.count(), 3)

    @override_settings(LLM_CHAT_MODELS=["openai/gpt-x"], LLM_DEFAULT_CHAT_MODEL="openai/gpt-x", LLM_EMBED_MODEL="")
    def test_import_with_keys(self):
        call_command("import_llm_settings", "--include-keys", stdout=StringIO())
        self.assertEqual(Provider.objects.get(name="openai").key, "sk-env")


@override_settings(**ENV)
class ChecksWithAdminModelsTests(Base):
    def test_admin_routed_models_are_not_flagged(self):
        from llm.checks import check_llm_settings
        LLMModel.objects.create(provider=bedrock(name="bedrock-gemma"), name="google.gemma-3-4b-it")
        ids = [i.id for i in check_llm_settings(None, databases=["default"])]
        self.assertNotIn("llm.W004", ids)


@override_settings(**ENV, RAG_MIN_SCORE=0.5, RAG_FAQ_MIN_SCORE=0.8, RAG_FAQ_CONTEXT_MIN_SCORE=0.75)
class EmbeddingCutoffTests(Base):
    def test_cutoffs_follow_the_active_embedding_model(self):
        config = llm.get_config()
        self.assertEqual((config.doc_cutoff, config.faq_cutoff, config.faq_context_cutoff), (0.5, 0.8, 0.75))
        ollama = Provider.objects.create(name="ollama", provider_type="ollama")
        LLMModel.objects.create(provider=ollama, name="emb-a", kind="embedding", min_score=0.2,
                                faq_min_score=0.55, faq_context_min_score=0.5)
        config = llm.get_config()
        self.assertEqual((config.doc_cutoff, config.faq_cutoff, config.faq_context_cutoff), (0.2, 0.55, 0.5))

    def test_cutoffs_validated(self):
        ollama = Provider.objects.create(name="ollama", provider_type="ollama")
        with self.assertRaisesMessage(ValidationError, "embedding models only"):
            LLMModel(provider=ollama, name="chat", kind="chat", min_score=0.3).full_clean()
        with self.assertRaisesMessage(ValidationError, "between -1 and 1"):
            LLMModel(provider=ollama, name="e", kind="embedding", min_score=3).full_clean()
