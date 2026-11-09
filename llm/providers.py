"""Provider types: which standard connection fields each provider needs, and how
they map onto LiteLLM. This is provider knowledge (LiteLLM's own conventions),
not model knowledge: models are always named in configuration.

Standard fields, used for every provider:
  key         primary credential (API key, or AWS access key ID)    encrypted
  secret      secondary credential (AWS secret key, Vertex SA JSON) encrypted
  endpoint    server URL
  region      cloud region
  api_version API version (Azure)
  project     cloud project (Vertex AI)
"""
from dataclasses import dataclass, field

REQUIRED, OPTIONAL = "required", "optional"

FIELD_LABELS = {
    "key": "Key",
    "secret": "Secret",
    "endpoint": "Endpoint",
    "region": "Region",
    "api_version": "API version",
    "project": "Project",
}
SECRET_FIELDS = ("key", "secret")
# Default LiteLLM keyword argument for each standard field.
DEFAULT_KWARGS = {"key": "api_key", "endpoint": "api_base", "api_version": "api_version"}


@dataclass(frozen=True)
class ProviderType:
    label: str
    chat_prefix: str | None          # LiteLLM provider prefix for chat models
    embed_prefix: str | None         # ... for embedding models (None = not supported)
    fields: dict                     # standard field -> REQUIRED / OPTIONAL
    kwargs: dict = field(default_factory=dict)   # standard field -> LiteLLM kwarg (overrides DEFAULT_KWARGS)
    hints: dict = field(default_factory=dict)    # standard field -> what to enter for this provider
    model_example: str = ""
    env_key: str = ""                # entry in settings.LLM_PROVIDERS used as fallback

    def kwarg(self, std_field: str) -> str:
        return self.kwargs.get(std_field) or DEFAULT_KWARGS[std_field]

    def prefix(self, kind: str) -> str | None:
        return self.embed_prefix if kind == "embedding" else self.chat_prefix


def _key(label="API key"):
    return {"key": label}


TYPES = {
    "ollama": ProviderType(
        "Ollama", "ollama_chat", "ollama", {"endpoint": OPTIONAL, "key": OPTIONAL},
        hints={"endpoint": "Default http://localhost:11434", "key": "Only if behind an authenticating proxy"},
        model_example="qwen2.5:3b or nomic-embed-text",
    ),
    "openai": ProviderType(
        "OpenAI", "openai", "openai", {"key": REQUIRED, "endpoint": OPTIONAL},
        hints={**_key(), "endpoint": "Only for a proxy/gateway"}, model_example="gpt-4o-mini",
    ),
    "anthropic": ProviderType(
        "Anthropic", "anthropic", None, {"key": REQUIRED, "endpoint": OPTIONAL},
        hints={**_key(), "endpoint": "Only for a proxy/gateway"}, model_example="claude-sonnet-4-5",
    ),
    "azure": ProviderType(
        "Azure OpenAI", "azure", "azure", {"key": REQUIRED, "endpoint": REQUIRED, "api_version": REQUIRED},
        hints={**_key(), "endpoint": "https://<resource>.openai.azure.com", "api_version": "e.g. 2024-10-21"},
        model_example="your deployment name",
    ),
    "gemini": ProviderType(
        "Google AI Studio (Gemini)", "gemini", "gemini", {"key": REQUIRED}, hints=_key(),
        model_example="gemini-2.5-flash",
    ),
    "vertex_ai": ProviderType(
        "Google Vertex AI", "vertex_ai", "vertex_ai",
        {"project": REQUIRED, "region": REQUIRED, "secret": OPTIONAL},
        kwargs={"project": "vertex_project", "region": "vertex_location", "secret": "vertex_credentials"},
        hints={"project": "GCP project ID", "region": "e.g. us-central1",
               "secret": "Service-account JSON (blank uses the server's default credentials)"},
        model_example="gemini-2.5-flash",
    ),
    "bedrock": ProviderType(
        "AWS Bedrock", "bedrock", "bedrock", {"key": REQUIRED, "secret": REQUIRED, "region": REQUIRED},
        kwargs={"key": "aws_access_key_id", "secret": "aws_secret_access_key", "region": "aws_region_name"},
        hints={"key": "AWS access key ID", "secret": "AWS secret access key", "region": "e.g. us-east-1"},
        model_example="google.gemma-3-4b-it or amazon.titan-embed-text-v2:0",
    ),
    "groq": ProviderType("Groq", "groq", None, {"key": REQUIRED}, hints=_key(), model_example="llama-3.3-70b-versatile"),
    "mistral": ProviderType("Mistral", "mistral", "mistral", {"key": REQUIRED}, hints=_key(), model_example="mistral-small-latest"),
    "cohere": ProviderType("Cohere", "cohere_chat", "cohere", {"key": REQUIRED}, hints=_key(), model_example="command-r"),
    "together_ai": ProviderType("Together AI", "together_ai", "together_ai", {"key": REQUIRED}, hints=_key()),
    "openrouter": ProviderType("OpenRouter", "openrouter", None, {"key": REQUIRED}, hints=_key(), model_example="<vendor>/<model>"),
    "deepseek": ProviderType("DeepSeek", "deepseek", None, {"key": REQUIRED}, hints=_key(), model_example="deepseek-chat"),
    "xai": ProviderType("xAI", "xai", None, {"key": REQUIRED}, hints=_key(), model_example="grok-3-mini"),
    "hosted_vllm": ProviderType(
        "vLLM server", "hosted_vllm", "hosted_vllm", {"endpoint": REQUIRED, "key": OPTIONAL},
        hints={"endpoint": "e.g. http://vllm-host:8000/v1", "key": "Only if the server requires one"},
    ),
    "openai_compatible": ProviderType(
        "Other OpenAI-compatible server", "openai", "openai", {"endpoint": REQUIRED, "key": OPTIONAL},
        hints={"endpoint": "e.g. http://localhost:1234/v1 (LM Studio, llama.cpp, a gateway...)",
               "key": "Only if the server requires one"},
    ),
}

CHOICES = [(name, t.label) for name, t in TYPES.items()]

# LiteLLM prefixes -> provider type (used to convert model strings).
PREFIX_TO_TYPE = {"ollama_chat": "ollama", "cohere_chat": "cohere"}
for _name in TYPES:
    if _name != "openai_compatible":
        PREFIX_TO_TYPE.setdefault(_name, _name)


def env_entry(type_name: str) -> dict:
    """Environment defaults for a provider type (from settings.LLM_PROVIDERS)."""
    from django.conf import settings

    t = TYPES[type_name]
    # The type's own entry, plus prefixes unique to it (e.g. "ollama_chat"); never
    # another type's entry (an OpenAI-compatible server must not get OpenAI's key).
    prefixes = [t.env_key or type_name] + [
        p for p in (t.chat_prefix, t.embed_prefix) if p and p != type_name and p not in TYPES
    ]
    entry = {}
    for prefix in prefixes:
        entry = {**settings.LLM_PROVIDERS.get(prefix, {}), **entry}
    entry.pop("litellm_provider", None)
    return entry


def form_schema() -> dict:
    """Per-type field requirements and hints, for the admin form's script."""
    return {
        name: {
            "fields": t.fields,
            "hints": t.hints,
            "embeddings": t.embed_prefix is not None,
            "example": t.model_example,
        }
        for name, t in TYPES.items()
    }
