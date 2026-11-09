"""Every admin-managed behaviour setting: type, section, scope and .env fallback.

Resolution: workspace override (if the setting is per-workspace and the
workspace sets it) -> platform setting (Django admin) -> Django setting (.env),
which only seeds the platform settings on first run.
"""
from dataclasses import dataclass
from typing import Callable

from django.conf import settings

TEXT, LINE, INT, FLOAT, BOOL, PERIOD, GROUNDING = "text", "line", "int", "float", "bool", "period", "grounding"
GROUNDING_MODES = [
    ("helpful", "Helpful: facts only from documents; general knowledge to understand questions and offer related info"),
    ("strict", "Strict: documents only, nothing else"),
]


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str
    section: str
    label: str
    default: Callable[[], object]   # value from Django settings (.env)
    per_workspace: bool = False
    help: str = ""
    min: float | None = None
    max: float | None = None


def _s(name):
    return lambda: getattr(settings, name)


def _pidgin(key):
    return lambda: settings.CHAT_PIDGIN_REPLIES[key]


WORDING, PERSONA, PIDGIN, ANSWERING, SEARCH, DOCUMENTS, DEFAULTS, SIGNUP = (
    "Assistant wording", "Persona (English)", "Persona (Nigerian Pidgin)", "Answering",
    "Search and FAQs", "Document processing", "Workspace defaults", "Sign-up",
)
PLACEHOLDERS = "Placeholders: {assistant} = chatbot name, {workspace} = workspace name."

SPECS = [
    # --- Assistant wording -----------------------------------------------------------
    Spec("system_prompt", TEXT, WORDING, "System prompt", _s("RAG_SYSTEM_PROMPT"), True,
         "Instructions sent to the model with every question. Placeholders: {assistant}, {workspace}, "
         "{grounding_rules} (the rules for the grounding mode below), {no_answer} (the not-in-the-documents reply)."),
    Spec("grounding_mode", GROUNDING, WORDING, "Grounding mode", _s("RAG_GROUNDING_MODE"), True,
         "Helpful lets the assistant use general knowledge to understand questions (places, synonyms, common "
         "sense) and point to related information; facts about the organisation still come only from documents."),
    Spec("helpful_rules", TEXT, WORDING, "Rules: helpful mode", _s("RAG_HELPFUL_RULES"),
         help="Inserted at {grounding_rules} for workspaces in helpful mode. Placeholders as above."),
    Spec("strict_rules", TEXT, WORDING, "Rules: strict mode", _s("RAG_STRICT_RULES"),
         help="Inserted at {grounding_rules} for workspaces in strict mode."),
    Spec("no_answer_message", LINE, WORDING, "Not in the documents", _s("RAG_NO_ANSWER_MESSAGE"), True,
         "Reply when the documents don't cover a question. " + PLACEHOLDERS),
    Spec("empty_kb_message", LINE, WORDING, "No documents yet", _s("RAG_EMPTY_KB_MESSAGE"), True, PLACEHOLDERS),
    Spec("token_limit_message", LINE, WORDING, "Token allowance used up", _s("CHAT_TOKEN_LIMIT_MESSAGE"), True,
         "{reset} = when the allowance resets. " + PLACEHOLDERS),
    Spec("no_embedding_message", LINE, WORDING, "No embedding model enabled", _s("RAG_NO_EMBEDDING_MESSAGE")),
    Spec("needs_reindex_message", LINE, WORDING, "Documents need reindexing", _s("RAG_NEEDS_REINDEX_MESSAGE")),
    # --- Persona -----------------------------------------------------------------------
    Spec("small_talk", BOOL, PERSONA, "Answer small talk directly", _s("CHAT_SMALL_TALK"), True,
         "Greetings, \"who are you?\", thanks, \"what can you do?\" and goodbye get the replies below, "
         "without search or a model call."),
    Spec("greeting_reply", LINE, PERSONA, "Greeting reply", _s("CHAT_GREETING_REPLY"), True, PLACEHOLDERS),
    Spec("identity_reply", LINE, PERSONA, "\"Who are you?\" reply", _s("CHAT_IDENTITY_REPLY"), True, PLACEHOLDERS),
    Spec("thanks_reply", LINE, PERSONA, "Thanks reply", _s("CHAT_THANKS_REPLY"), True, PLACEHOLDERS),
    Spec("help_reply", LINE, PERSONA, "\"What can you do?\" reply", _s("CHAT_HELP_REPLY"), True, PLACEHOLDERS),
    Spec("goodbye_reply", LINE, PERSONA, "Goodbye reply", _s("CHAT_GOODBYE_REPLY"), True, PLACEHOLDERS),
    Spec("pidgin_greeting_reply", LINE, PIDGIN, "Greeting reply (Pidgin)", _pidgin("greeting"), True, PLACEHOLDERS),
    Spec("pidgin_identity_reply", LINE, PIDGIN, "\"Who you be?\" reply (Pidgin)", _pidgin("identity"), True, PLACEHOLDERS),
    Spec("pidgin_thanks_reply", LINE, PIDGIN, "Thanks reply (Pidgin)", _pidgin("thanks"), True, PLACEHOLDERS),
    Spec("pidgin_help_reply", LINE, PIDGIN, "\"Wetin you fit do?\" reply (Pidgin)", _pidgin("help"), True, PLACEHOLDERS),
    Spec("pidgin_goodbye_reply", LINE, PIDGIN, "Goodbye reply (Pidgin)", _pidgin("goodbye"), True, PLACEHOLDERS),
    Spec("pidgin_no_answer_message", LINE, PIDGIN, "Not in the documents (Pidgin)", _pidgin("no_answer"), True,
         PLACEHOLDERS),
    # --- Answering -------------------------------------------------------------------------
    Spec("temperature", FLOAT, ANSWERING, "Temperature", _s("RAG_TEMPERATURE"), True,
         "Lower keeps answers closer to the documents.", 0, 2),
    Spec("top_k", INT, ANSWERING, "Passages per answer", _s("RAG_TOP_K"), True,
         "Most document passages given to the model for one question.", 1, 50),
    Spec("history_limit", INT, ANSWERING, "Conversation history", _s("CHAT_HISTORY_LIMIT"), True,
         "Most earlier messages sent to the model as context.", 0, 200),
    Spec("max_message_chars", INT, ANSWERING, "Max question length", _s("CHAT_MAX_MESSAGE_CHARS"), True,
         "Characters.", 1, 100000),
    Spec("rewrite_queries", BOOL, ANSWERING, "Rewrite questions for search", _s("RAG_REWRITE_QUERIES"), True,
         "Before searching, the chat model turns the question into a plain-English search query (handles Pidgin, "
         "paraphrases like \"what time do you close\", and follow-ups). One small extra model call per question."),
    # --- Search and FAQs (global) -------------------------------------------------------------
    Spec("rewrite_prompt", TEXT, SEARCH, "Question rewriting instructions", _s("RAG_REWRITE_PROMPT")),
    Spec("confident_factor", FLOAT, SEARCH, "Strong match factor", _s("RAG_CONFIDENT_FACTOR"),
         help="Passages scoring at least this many times the relevance cut-off skip the key-word check.",
         min=1, max=10),
    Spec("min_term_overlap", FLOAT, SEARCH, "Key-word coverage required", _s("RAG_MIN_TERM_OVERLAP"),
         help="Share of the question's key words the passages must contain before the model is called "
              "(0 turns the check off).", min=0, max=1),
    Spec("keyword_min_coverage", FLOAT, SEARCH, "Keyword search coverage", _s("RAG_KEYWORD_MIN_COVERAGE"),
         help="Passages containing at least this share of the question's key words are found even with low "
              "similarity (names, codes). 0 turns keyword search off.", min=0, max=1),
    Spec("faq_context_max", INT, SEARCH, "Close FAQs given as context", _s("RAG_FAQ_CONTEXT_MAX"), min=0, max=10),
    Spec("min_score", FLOAT, SEARCH, "Default document relevance cut-off", _s("RAG_MIN_SCORE"),
         help="Used when the embedding model has no cut-off of its own (LLM models).", min=-1, max=1),
    Spec("faq_min_score", FLOAT, SEARCH, "Default FAQ match cut-off", _s("RAG_FAQ_MIN_SCORE"), min=-1, max=1),
    Spec("faq_context_min_score", FLOAT, SEARCH, "Default FAQ context cut-off", _s("RAG_FAQ_CONTEXT_MIN_SCORE"),
         min=-1, max=1),
    # --- Document processing (global) -----------------------------------------------------------
    Spec("chunk_size", INT, DOCUMENTS, "Chunk size", _s("RAG_CHUNK_SIZE"),
         help="Characters per chunk. Changing it applies to documents (re)built afterwards.", min=100, max=20000),
    Spec("chunk_overlap", INT, DOCUMENTS, "Chunk overlap", _s("RAG_CHUNK_OVERLAP"),
         help="Characters shared by neighbouring chunks; must be smaller than the chunk size.", min=0, max=10000),
    Spec("embed_batch_size", INT, DOCUMENTS, "Embedding batch size", _s("RAG_EMBED_BATCH_SIZE"),
         help="Chunks sent per embedding request.", min=1, max=512),
    # --- Workspace defaults (global; each workspace can set its own on its page) -----------------
    Spec("default_max_upload_mb", INT, DEFAULTS, "Max file size (MB)", _s("RAG_MAX_UPLOAD_MB"), min=1, max=1000),
    Spec("default_max_documents", INT, DEFAULTS, "Max documents per workspace", _s("RAG_MAX_DOCUMENTS"), min=0),
    Spec("default_allowed_file_types", LINE, DEFAULTS, "Allowed file types",
         lambda: ",".join(settings.RAG_ALLOWED_EXTENSIONS), help="Comma-separated, from txt, pdf, md, csv, docx."),
    Spec("default_token_allowance", INT, DEFAULTS, "Token allowance", _s("CHAT_TOKEN_ALLOWANCE"),
         help="Per period; 0 = unlimited.", min=0),
    Spec("default_token_period", PERIOD, DEFAULTS, "Allowance period", _s("CHAT_TOKEN_PERIOD")),
    Spec("default_max_output_tokens", INT, DEFAULTS, "Max output tokens per answer", _s("CHAT_MAX_OUTPUT_TOKENS"),
         help="0 = model default.", min=0),
    # --- Sign-up (global) ---------------------------------------------------------------------------
    Spec("allow_signup", BOOL, SIGNUP, "Allow sign-up", _s("PORTAL_SIGNUP_ENABLED"),
         help="Anyone can create an account from the home page and create free chatbots."),
    Spec("free_chatbots_per_account", INT, SIGNUP, "Free chatbots per account", _s("PORTAL_FREE_CHATBOTS_PER_ACCOUNT"),
         help="Workspaces an account may create itself. Superusers are not limited. Free workspaces use the "
              "Workspace defaults limits above.", min=0, max=100),
]
BY_NAME = {s.name: s for s in SPECS}
WORKSPACE_SPECS = [s for s in SPECS if s.per_workspace]
SECTIONS = list(dict.fromkeys(s.section for s in SPECS))
