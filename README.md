# saas_chat

Multi-tenant Django platform. Each customer (a **Tenant**) gets its own
independent workspace. The public startup page routes customers to their
workspace, whose main app is a streaming chat that answers **only from the
workspace's own documents**. All model access goes through
[LiteLLM](https://docs.litellm.ai), so models and providers are configuration,
not code.

## Settings (Django admin)

Behaviour is configured in the Django admin, not `.env`:

- **Settings > Platform settings**: one page for all workspaces, grouped into
  *Assistant wording* (system prompt, "not in the documents", no documents,
  token-limit, no embedding model, needs-reindex messages), *Persona* (English
  and Nigerian Pidgin small-talk replies, small talk on/off), *Answering*
  (temperature, passages per answer, history length, max question length),
  *Search and FAQs* (keyword/term-overlap rules, FAQ context, default cut-offs),
  *Document processing* (chunk size/overlap, embedding batch size) and
  *Workspace defaults* (upload and token limits for workspaces that don't set
  their own).
- **Per workspace** (*Tenants* > a workspace): its chatbot name, upload and
  token limits, and *Settings overrides* for the wording, persona and
  answering settings. Blank overrides use the platform value.
- Resolution: workspace override -> platform setting -> `.env` value (which
  only seeds the platform settings on first run). Changes apply without a
  restart (cached per process for `SITECONFIG_CACHE_SECONDS`, default 30).
- Elsewhere in the admin: LLM providers and models (with per-embedding-model
  relevance cut-offs), FAQs, website widgets, users and memberships.

`.env` keeps secrets and infrastructure: `DJANGO_SECRET_KEY`, database, hosts,
`LLM_ENCRYPTION_KEY`, provider credentials, `WIDGET_TRUST_X_FORWARDED_FOR`.

## Home page, sign-up and free chatbots

- `/` is public: a landing page promoting the service (grounded answers,
  website widget, English/Pidgin persona, performance) with a sign-in panel
  and the free plan (from *Platform settings > Workspace defaults*). Signed-in
  users get a dashboard of their workspaces (Open chat / Manage).
- `/signup/`: self-service accounts (username, email, password; spam trap;
  5 sign-ups per IP per hour), then `/create/` to create a chatbot: a new
  workspace with its chatbot name, the creator as workspace admin.
- *Platform settings > Sign-up*: *Allow sign-up* and *Free chatbots per
  account* (default 1; superusers are not limited). Free workspaces use the
  *Workspace defaults* limits.

## Accounts and access

Every page requires login (`/accounts/login/`), except the public website
widget. Users are Django users, managed in the Django admin (*Users*):

- **Superusers** can open every workspace and the Django admin.
- Other users get access through **memberships** (edit them on the user's
  page under *Workspace access*, or on a workspace's page under *Members*):
  - *Member*: the workspace chat.
  - *Workspace admin*: the chat plus the Admin tabs (Documents, Performance,
    Embed).
- A workspace you're not a member of returns 404; members get 403 on Admin
  pages. The home page lists your workspaces (or opens your only one).
- Chat conversations are private to the user who started them.

Each workspace also has a **chatbot name** (*Tenants* > *Assistant*, default
"Tosin"). The assistant uses it in its instructions (`{assistant}` in
`RAG_SYSTEM_PROMPT`), the chat page greets with it, and the widget's message
box and default title use it.

The assistant keeps that persona: messages that are only small talk (a
greeting or its name, "who are you?", "are you Tosin?", thanks, "what can you
do?", goodbye) get a persona reply without search or a model call
(`CHAT_GREETING_REPLY`, `CHAT_IDENTITY_REPLY`, `CHAT_THANKS_REPLY`,
`CHAT_HELP_REPLY`, `CHAT_GOODBYE_REPLY`; `{assistant}`/`{workspace}`
placeholders; `CHAT_SMALL_TALK=0` turns it off). When a question addresses it
by name ("Hi Tosin, how much leave do I get?"), the name and greeting are
removed before FAQ matching and document search, while the model still sees
the whole message. The system prompt tells the model its name and that
messages addressing it by name are speaking to it.

**Nigerian Pidgin.** Messages in Pidgin are recognised: Pidgin small talk
("How far Tosin", "Wetin be your name?", "Tank you o", "I dey go"), Pidgin
function words (abeg, wetin, dey, una, fit…) are ignored as search terms, and
fixed replies (small talk, "not in the documents") come from
`CHAT_PIDGIN_REPLIES` (JSON override). For questions, the model is told to
answer in Pidgin. Pidgin quality depends on the chat model: small models
manage simple answers; larger ones are noticeably better.

## URL layout

| Path                          | What it is                                         |
|-------------------------------|----------------------------------------------------|
| `/`                           | Public startup page: "Go to your workspace" lookup |
| `/t/<slug>/`                  | Workspace entry point (redirects to the chat)      |
| `/t/<slug>/chat/`             | The workspace's chat app                           |
| `/t/<slug>/chat/c/<id>/`      | One conversation                                   |
| `/t/<slug>/chat/api/send/`    | POST: send a message, streams the reply as NDJSON  |
| `/t/<slug>/admin/`            | Workspace admin: knowledge-base report and uploads |
| `/admin/`                     | Platform administration                            |

The `t` prefix is configurable with `TENANT_URL_PREFIX`.

## Apps

- `portal`: public landing page and workspace lookup.
- `tenants`: the `Tenant` model and the `@tenant_required` decorator.
- `llm`: the only place models are called. A thin LiteLLM adapter for
  streaming chat and embeddings that routes each model to its provider's
  settings, plus startup checks for the LLM configuration.
- `chat`: conversations, messages, the grounded send endpoint and the chat UI.
- `rag`: the workspace knowledge base. Uploaded documents are stored, their
  text extracted (PDF, DOCX, TXT, MD, CSV), split into overlapping chunks and
  embedded with the configured embedding model. Chunks and their vectors are
  stored per tenant; `rag/retrieval.py` does the similarity search.

The chat and admin pages share a **Chat / Admin** toggle.

## Knowledge base (RAG)

`/t/<slug>/admin/` shows a report (documents consumed, chunks available), an
upload area (drag and drop, several files at once) and the document list with
status and a remove action.

**Documents vs chunks.** Documents are what you manage; chunks are generated
from them. On the workspace Admin page you can delete a document or *Rebuild*
one that failed or was indexed with outdated embedding settings. In the Django
admin, *Documents* supports rename, delete (single or bulk) and *Rebuild
chunks*; *Chunks* is read-only (browse/search, plus *Rebuild their
documents*), so counts and the index can't drift. Deleting a document from
anywhere, including deleting its workspace, also deletes its chunks and its
stored file. Report counts are computed from the chunks that exist.

**Upload limits** are set per workspace in the Django admin (*Tenants* >
*Knowledge base limits*): max file size (MB), max documents (including failed
ones) and allowed file types (from txt, pdf, md, csv, docx). Blank fields use
the platform defaults `RAG_MAX_UPLOAD_MB` (5), `RAG_MAX_DOCUMENTS` (3) and
`RAG_ALLOWED_EXTENSIONS` (txt,pdf). A batch larger than the free slots is
refused as a whole; new limits don't affect documents already stored. Uploads are processed during the request, so large
files take a moment. Duplicate files (same content) are rejected per workspace.
Files are stored under `MEDIA_ROOT/rag/<slug>/` and are never served over HTTP.
Scanned PDFs without a text layer fail with a clear message; they need OCR.

## Performance report

Each workspace's Admin page has a **Performance** tab
(`/t/<slug>/admin/performance/`), with one period filter (7 / 30 / 90 days,
all time) scoping everything:

- **Key figures**: questions, answer rate, retrieval hit rate (searches that
  found passages), median and 95th-percentile response time, tokens used.
- **Outcomes** of every question: *answered*; *declined by model* (passages
  were found but didn't contain the answer: a retrieval precision problem);
  *not found in documents* (nothing relevant; no model call: a coverage or
  threshold problem); *unavailable or error*. Plus grounded precision
  (answered / passages found), citation rate and tokens per answer.
- **Charts** (hover or keyboard focus for details; each has a data table):
  questions per day by outcome; response time median and p95; best-passage
  relevance histogram with the `RAG_MIN_SCORE` cut-off, to calibrate it (many
  misses just below the line mean it is too strict); most used documents.
- **Model efficiency**: per chat model calls, answer rate, median time,
  average tokens in/out and output tokens per second; embedding model search
  time and index size.
- **Questions the documents couldn't answer**: what to upload next.
- **Document processing**: every upload, rebuild and reindex is recorded
  (`IndexingRun`) with size, extracted characters, chunks, extract / embed /
  total time, result and embedding model; the tab shows documents processed,
  failures, median and longest processing time, chunks indexed, embedding
  speed (chunks/s) and the 10 most recent runs. Records outlive deleted
  documents. The Documents tab shows each document's latest *Indexed in*
  time. Deleting a document while it is being processed stops the work after
  the current embedding batch (recorded as *Cancelled*).

Each answer records its outcome, search time, best-passage similarity and
passage count; answers from before this was added were classified from their
stored content.

## Website widget

Each workspace's Admin page has an **Embed** tab (`/t/<slug>/admin/embed/`) to
put the assistant on the customer's own website:

- Settings: on/off, title, greeting, accent colour (button text switches
  between white and black for contrast), corner, **websites allowed to embed
  it** (one per line: `example.com`, `*.example.com`, `localhost:8000`) and a
  per-visitor rate limit (messages per minute per IP, default 10).
- A one-line snippet to paste before `</body>`:
  `<script src="https://<host>/static/widget/embed.js" data-key="<key>" async></script>`.
  It adds a chat button (in a Shadow DOM, so the site's CSS can't break it)
  that opens the chat in an iframe served from this platform.
- When on, the tab itself shows the real widget as a live preview.
- *Generate a new key* invalidates the old snippet immediately.

Visitors get the same grounded answers (FAQs, document search, token
allowance) through the workspace's default model. Safeguards: the chat page
sends `Content-Security-Policy: frame-ancestors` with the allowed websites, so
browsers won't show it elsewhere; the API rejects cross-origin browser calls
and non-JSON requests; each visitor (a random ID kept in their browser) can
only continue their own conversations; requests are rate-limited per IP.
Widget conversations stay out of the workspace chat's sidebar and are counted
in the Performance report. Staff can see and switch off any widget in the
Django admin (*Website widget*).

For production: serve over HTTPS, put the platform's host in
`DJANGO_ALLOWED_HOSTS`, use a shared cache (e.g. Redis) so rate limits apply
across server processes, and set `WIDGET_TRUST_X_FORWARDED_FOR=1` only behind
a proxy that sets that header.

## Token limits

Each workspace can have chat token limits, set in the Django admin (*Tenants* >
*Chat token limits*); blank fields use the platform defaults:

| Field | Default setting | Meaning |
|---|---|---|
| Token allowance | `CHAT_TOKEN_ALLOWANCE` (0 = unlimited) | Input + output tokens of model calls per period |
| Allowance period | `CHAT_TOKEN_PERIOD` (`month`) | `day`, `week` (from Monday) or `month`; resets at midnight |
| Max output tokens per answer | `CHAT_MAX_OUTPUT_TOKENS` (0 = model default) | Passed to the model as `max_tokens` |

When the allowance is used up, questions that would need the model get
`CHAT_TOKEN_LIMIT_MESSAGE` (with the reset date) and are recorded with the
outcome "Token allowance used up"; FAQ answers and "not found" replies use no
tokens and keep working. Usage is shown in the Tenants list and as a meter on
the workspace's Performance tab. The limit is checked before each model call,
so the call that crosses it completes; tokens are counted as reported by the
provider.

## FAQs

Staff manage per-workspace FAQs in the Django admin (*Knowledge base* >
*FAQs*): workspace, question, optional other phrasings (one per line), answer
(Markdown), enabled. A chat question that matches an FAQ gets its answer
directly: **no document search and no model call** (shown in the chat as
"Answered from FAQ", counted under its own outcome in the Performance report,
and in the FAQ's *Times used*).

A question matches when it is the same as the question or a phrasing
(ignoring case and punctuation), or when it uses no key words that the
phrasing lacks, covers at least half of the phrasing's key words, has the
same negation, and its embedding similarity is at least `RAG_FAQ_MIN_SCORE`
(0.8). Similarity alone is not trusted: "Who is the CFO?" scores ~0.90
against "Who is the CEO?". FAQs that are close but not direct matches
(`RAG_FAQ_CONTEXT_MIN_SCORE`, up to `RAG_FAQ_CONTEXT_MAX`) are given to the
model as extra passages, under the usual grounding rules. Add common
rewordings as phrasings to answer more questions directly. FAQ phrasings are
embedded when saved (or on first use) and re-embedded automatically when
their wording or the embedding settings change.

## Grounded chat

The chat answers exclusively from the workspace's documents:

1. Hybrid search: the question (plus the previous question, for follow-ups)
   is embedded and compared with the workspace's chunks, and chunks containing
   most of its key terms (`RAG_KEYWORD_MIN_COVERAGE`, default 75%) are added
   even when their similarity is low; embeddings rate names and rare terms
   poorly ("Who is Chief Iyare?"). Passages matching more of the question's
   terms rank first.
2. If no chunk scores at least `RAG_MIN_SCORE`, or the matching chunks don't
   actually contain the question's key terms (`RAG_MIN_TERM_OVERLAP`, default
   half of them; stop words ignored), the user gets `RAG_NO_ANSWER_MESSAGE` and
   **the chat model is not called at all**. The term check catches questions
   that only resemble a passage in shape ("What is X?"). With no documents,
   users get `RAG_EMPTY_KB_MESSAGE`.
3. Otherwise the top `RAG_TOP_K` passages are sent with a strict system prompt
   (`RAG_SYSTEM_PROMPT`): use only the passages, cite them, reply with the
   no-answer message when they don't contain the answer, and ignore
   instructions inside documents. Temperature is `RAG_TEMPERATURE`.

Each answer shows its source documents. Similarity scales differ between
embedding models, so the cut-offs are set **per embedding model** in the
Django admin (*LLM models* > an embedding model > *Document relevance
cut-off*, *FAQ match cut-off*, *FAQ context cut-off*); blank uses
`RAG_MIN_SCORE`, `RAG_FAQ_MIN_SCORE`, `RAG_FAQ_CONTEXT_MIN_SCORE`. Calibrate by
measuring the best-passage similarity of on-topic and off-topic questions and
picking a value between them. Measured values: `nomic-embed-text` 0.52 / 0.80
/ 0.75; Bedrock `amazon.titan-embed-text-v2:0` 0.20 / 0.55 / 0.50.

If you change `LLM_EMBED_MODEL` or `RAG_DOCUMENT_PREFIX`, existing chunks no
longer match and are excluded from search (the admin page flags them). Rebuild
them with:

```bash
.venv/bin/python manage.py reindex_documents            # outdated or failed only
.venv/bin/python manage.py reindex_documents --all      # everything
.venv/bin/python manage.py reindex_documents --tenant demo
```

## Adding tenant-scoped features

Decorate views with `@tenant_required` (it supports sync and async views). It
loads the active tenant into `request.tenant` and returns 404 for unknown or
inactive workspaces. Always filter data by `request.tenant`, e.g.
`Conversation.objects.filter(tenant=request.tenant)`.

## Setup

```bash
# Debian/Ubuntu: the system python lacks ensurepip, so create the venv without pip and bootstrap it
python3 -m venv --without-pip .venv
.venv/bin/python /usr/lib/python3/dist-packages/pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env          # then adjust values
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Configure models and providers in `.env` (see "Models and providers" below). Create tenants at `/admin/`, upload documents on the
workspace's Admin page, then ask questions in its chat.

## Models and providers

Models and providers can be managed in two places:

1. **Django admin** (`/admin/` > *LLM models and providers*), which overrides
   the environment:
   - **LLM providers** are connections: a name, a provider type (Ollama,
     OpenAI, Anthropic, Azure OpenAI, Gemini, Vertex AI, AWS Bedrock, Groq,
     Mistral, Cohere, Together AI, OpenRouter, DeepSeek, xAI, vLLM, or any
     OpenAI-compatible server) and standard connection fields. The form shows
     only the fields the chosen type uses and requires the necessary ones:

     | Field | Meaning | Required for |
     |---|---|---|
     | Key | API key, or AWS access key ID | hosted APIs, Bedrock |
     | Secret | AWS secret access key, or Vertex service-account JSON | Bedrock (Vertex optional) |
     | Endpoint | Server URL | Azure, vLLM, OpenAI-compatible (Ollama optional) |
     | Region | Cloud region | Bedrock, Vertex AI |
     | API version | | Azure |
     | Project | | Vertex AI |

     Key and Secret are encrypted at rest (`LLM_ENCRYPTION_KEY`, or derived
     from `DJANGO_SECRET_KEY`) and never displayed. Blank fields fall back to
     the provider type's environment variables. *Advanced* options take extra
     LiteLLM arguments (not credentials).
   - **LLM models** pick a provider and name the model there (e.g. provider
     `bedrock`, model `google.gemma-3-4b-it`). The model ID used by the app is
     `<provider name>/<model>`. One Ollama provider serves both chat and
     embedding models. Models of a disabled or incomplete provider are hidden.
   - If the admin has any chat-model rows they are the chat list, otherwise
     `LLM_CHAT_MODELS` is used; the same applies to the embedding model.
     Changes apply without a restart (within `LLM_CONFIG_CACHE_SECONDS`).
   - *Test selected models* sends a tiny real request. After changing the
     embedding model, use *Reindex selected documents* on Documents.
   - `manage.py import_llm_settings` copies the environment configuration into
     the admin (keys only with `--include-keys`).
2. **Environment / Django settings** (`saas_chat/settings.py`, filled from
   `.env`), the defaults:

- **Models** are LiteLLM model strings, `<provider>/<model>`:
  `LLM_CHAT_MODELS` (the allowlist shown in the chat), `LLM_DEFAULT_CHAT_MODEL`
  and `LLM_EMBED_MODEL`. Providers can be mixed, e.g.
  `LLM_CHAT_MODELS=ollama_chat/qwen2.5:3b,openai/gpt-4o-mini,anthropic/claude-sonnet-4-5`.
- **Providers**: `LLM_PROVIDERS` maps each provider prefix to its credentials
  and endpoint, read from the provider's usual variables: `OLLAMA_API_BASE`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_API_KEY` / `AZURE_API_BASE` /
  `AZURE_API_VERSION`, `GEMINI_API_KEY`, `VERTEXAI_*`, `AWS_*` (Bedrock),
  `GROQ_API_KEY`, `MISTRAL_API_KEY`, `COHERE_API_KEY`, `TOGETHERAI_API_KEY`,
  `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`,
  `HOSTED_VLLM_API_BASE`, and `OPENAI_COMPATIBLE_API_BASE` for any
  OpenAI-compatible server (models `openai_compatible/<name>`). Each provider
  also takes `<PROVIDER>_EXTRA_PARAMS` (JSON) for provider-specific options,
  e.g. `OLLAMA_EXTRA_PARAMS={"num_ctx": 8192}`.
- **Anything else**: `LLM_PROVIDERS_EXTRA` (JSON) adds or overrides providers.
  An entry with `"litellm_provider"` is a named alias, e.g. a second Ollama
  server: `{"gpu_box": {"litellm_provider": "ollama_chat", "api_base": "http://gpu-box:11434"}}`,
  then models `gpu_box/<name>`. `LLM_MODEL_PARAMS` (JSON) sets per-model options.
- Common: `LLM_TIMEOUT`, `LLM_NUM_RETRIES`, `LLM_TEMPERATURE`.

For each call the adapter merges: common settings, then the provider's entry,
then the model's `LLM_MODEL_PARAMS`. `manage.py check` (also run by
`runserver`) reports models without a recognisable provider and providers
with no credentials configured. Django's error pages mask the keys.

Changing `LLM_EMBED_MODEL` requires `manage.py reindex_documents`.

## Streaming and ASGI

Chat views are async and stream responses. `daphne` is listed first in
`INSTALLED_APPS`, so `manage.py runserver` serves ASGI and streams correctly.
In production, run an ASGI server against `saas_chat.asgi:application`
(e.g. `daphne saas_chat.asgi:application`). Under WSGI, replies would be
buffered and arrive all at once. If you put nginx in front, the send endpoint
already sets `X-Accel-Buffering: no`.

## Configuration

All configuration lives in `saas_chat/settings.py` and is read from
environment variables, optionally loaded from `.env`. Real environment
variables take precedence over `.env`, and blank values fall back to the
defaults. Application code reads `django.conf.settings`, never `os.environ`.
`.env.example` lists every variable; the main ones are:

| Variable                  | Default                  | Notes                                     |
|---------------------------|--------------------------|-------------------------------------------|
| `DJANGO_SECRET_KEY`       | insecure dev value       | **Required** when `DJANGO_DEBUG=0`        |
| `DJANGO_DEBUG`            | `1`                      | Set to `0` in production                  |
| `DJANGO_ALLOWED_HOSTS`    | `localhost,127.0.0.1`    | Comma-separated                           |
| `DJANGO_DB_*`             | SQLite in project root   | Engine, name, user, password, host, port  |
| `LLM_CHAT_MODELS`         | (none)                   | Allowlist of LiteLLM chat model strings   |
| `LLM_DEFAULT_CHAT_MODEL`  | first allowlisted model  | Preselected model                         |
| `LLM_EMBED_MODEL`         | (none)                   | LiteLLM embedding model string            |
| `<PROVIDER>_*`            | (empty)                  | Per-provider keys/endpoints; see above    |
| `LLM_TIMEOUT`             | `300`                    | Seconds before a model call is abandoned  |
| `RAG_QUERY_PREFIX` / `RAG_DOCUMENT_PREFIX` | (empty) | Instruction prefixes some embedding models need |
| `RAG_MIN_SCORE`           | `0.5`                    | Relevance cut-off; calibrate per model    |
| `RAG_MIN_TERM_OVERLAP`    | `0.5`                    | Share of question terms passages must contain; 0 disables |
| `RAG_KEYWORD_MIN_COVERAGE` | `0.75`                  | Keyword route: share of question terms a chunk must contain; 0 disables |
| `RAG_TOP_K`               | `5`                      | Passages given to the model               |
| `RAG_TEMPERATURE`         | `0.1`                    | Temperature for grounded answers          |
| `RAG_SYSTEM_PROMPT`       | strict grounding prompt  | Supports `{workspace}`, `{no_answer}`     |
| `RAG_NO_ANSWER_MESSAGE`   | "I couldn't find that…"  | Reply when documents don't cover it       |
| `CHAT_HISTORY_LIMIT`      | `20`                     | Previous messages sent as context         |
| `CHAT_MAX_MESSAGE_CHARS`  | `8000`                   | Maximum length of one user message        |
| `DJANGO_MEDIA_ROOT`       | `media/` in project root | Where uploaded documents are stored       |
| `RAG_CHUNK_SIZE`          | `1000`                   | Characters per chunk                      |
| `RAG_CHUNK_OVERLAP`       | `150`                    | Characters shared by neighbouring chunks  |
| `RAG_MAX_UPLOAD_MB`       | `5`                      | Default largest file (per-workspace override) |
| `RAG_MAX_DOCUMENTS`       | `3`                      | Default documents per workspace (override) |
| `RAG_ALLOWED_EXTENSIONS`  | `txt,pdf`                | Default file types (override)             |

## Known gaps

- No self-service sign-up or password reset: staff create users and set
  passwords in the Django admin (users can change their own at
  `/accounts/password/`). Add user accounts linked to tenants before onboarding
  real customers.

## Tests

```bash
.venv/bin/python manage.py test
```

Model calls are mocked at the `llm` boundary (and LiteLLM itself in
`llm/tests.py`), so no running model is needed.
