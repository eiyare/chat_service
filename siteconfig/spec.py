"""Every admin-managed behaviour setting: type, section, scope and .env fallback.

Resolution: workspace override (if the setting is per-workspace and the
workspace sets it) -> platform setting (Django admin) -> Django setting (.env),
which only seeds the platform settings on first run.
"""
from dataclasses import dataclass
from typing import Callable

from django.conf import settings

TEXT, LINE, INT, FLOAT, BOOL, PERIOD, GROUNDING = "text", "line", "int", "float", "bool", "period", "grounding"
SECRET = "secret"  # stored encrypted (llm.crypto); the admin never shows it again
CHOICE = "choice"  # one of Spec.choices
SMTP_SECURITY = [
    ("starttls", "STARTTLS (usually port 587)"),
    ("ssl", "SSL/TLS (usually port 465)"),
    ("none", "None (only for a mail server on the same machine or network)"),
]
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
    optional: bool = False  # text settings: may be left blank on the platform too
    choices: tuple = ()     # CHOICE settings: ((value, label), ...)


def _s(name):
    return lambda: getattr(settings, name)


def _pidgin(key):
    return lambda: settings.CHAT_PIDGIN_REPLIES[key]


WORDING, PERSONA, PIDGIN, ANSWERING, SEARCH, DOCUMENTS, DEFAULTS, SIGNUP, CONTACT, EMAIL, TRAFFIC = (
    "Assistant wording", "Persona (English)", "Persona (Nigerian Pidgin)", "Answering",
    "Search and FAQs", "Document processing", "Workspace defaults", "Site and sign-up", "Contact", "Email (SMTP)",
    "Traffic",
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
    Spec("student_tools_prompt", TEXT, WORDING, "Instructions: Moodle student tools", _s("RAG_STUDENT_TOOLS_PROMPT"),
         True, "Added to the system prompt when the assistant can look up a Moodle student's own grades, "
               "completion and deadlines (website widget > Student data tools)."),
    Spec("app_tools_prompt", TEXT, WORDING, "Instructions: app tools", _s("RAG_APP_TOOLS_PROMPT"), True,
         "Added to the system prompt when connected apps offer tools (e.g. order lookup on your website). "
         "Moodle's student tools use their own instructions above."),
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
    Spec("llm_timeout", INT, ANSWERING, "Model timeout (seconds)", lambda: int(settings.LLM_TIMEOUT), min=5, max=1800,
         help="How long to wait for a model to start answering (or for an embedding) before giving up. Slow "
              "local models may need longer."),
    Spec("max_message_chars", INT, ANSWERING, "Max question length", _s("CHAT_MAX_MESSAGE_CHARS"), True,
         "Characters.", 1, 100000),
    Spec("rewrite_queries", BOOL, ANSWERING, "Rewrite questions for search", _s("RAG_REWRITE_QUERIES"), True,
         "Before searching, the chat model turns the question into a plain-English search query (handles Pidgin, "
         "paraphrases like \"what time do you close\", and follow-ups). One small extra model call per question."),
    Spec("provider_retries", INT, ANSWERING, "Retries after a provider error", _s("LLM_RETRIES"),
         help="When the model provider fails with a temporary error (busy/overloaded, rate limited, timed out, "
              "unreachable) before any of the reply has been shown, try again this many times, waiting about 1, "
              "2, 4 ... seconds in between. 0 shows the error straight away.", min=0, max=5),
    # --- Search and FAQs (global) -------------------------------------------------------------
    Spec("rewrite_prompt", TEXT, SEARCH, "Question rewriting instructions", _s("RAG_REWRITE_PROMPT")),
    Spec("confident_factor", FLOAT, SEARCH, "Strong match factor", _s("RAG_CONFIDENT_FACTOR"),
         help="Passages scoring at least this many times the relevance cut-off skip the key-word check "
              "(used when the embedding model has no Strong match cut-off of its own).",
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
    Spec("section_chunking", BOOL, DOCUMENTS, "Section-aware chunking", _s("RAG_SECTION_CHUNKING"),
         help="Split documents at their headings (\"1. Opening Hours\") so chunks don't mix topics, and add the "
              "document title and section to each chunk. Applies to documents (re)built afterwards. Works best for "
              "documents with clearly numbered or Markdown headings; tables of contents and numbered lists can be "
              "mistaken for headings."),
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
    Spec("site_name", LINE, SIGNUP, "Site name", _s("SITE_NAME"),
         help="The platform's name on the public site, in page titles and in emails."),
    Spec("sidebar_limit", INT, SIGNUP, "Chats listed in the sidebar", _s("CHAT_SIDEBAR_LIMIT"), min=5, max=500,
         help="How many of a user's recent chats the workspace chat lists."),
    Spec("site_url", LINE, SIGNUP, "Public address", lambda: "", optional=True,
         help="The site's address as visitors see it, e.g. https://saaschat.tech. Used for links in emails "
              "(such as confirming an email address). Blank: the address the request came in on, which behind a "
              "proxy may be http:// or an internal name."),
    Spec("require_email_verification", BOOL, SIGNUP, "Confirm email addresses at sign-up", lambda: True,
         help="New accounts stay inactive until their owner clicks a link emailed to them. Needs the Email (SMTP) "
              "settings. Accounts made in Django admin are not affected."),
    Spec("verification_link_hours", INT, SIGNUP, "Confirmation links last (hours)", lambda: 48, min=1, max=720),
    Spec("unverified_account_days", INT, SIGNUP, "Delete unconfirmed accounts after (days)", lambda: 7, min=0,
         max=365, help="Frees their usernames and email addresses. 0 keeps them."),
    Spec("allow_signup", BOOL, SIGNUP, "Allow sign-up", _s("PORTAL_SIGNUP_ENABLED"),
         help="Anyone can create an account from the home page and create free chatbots."),
    Spec("free_chatbots_per_account", INT, SIGNUP, "Free chatbots per account", _s("PORTAL_FREE_CHATBOTS_PER_ACCOUNT"),
         help="Workspaces an account may create itself. Superusers are not limited. Free workspaces use the "
              "Workspace defaults limits above.", min=0, max=100),
    # --- Contact (global) ---------------------------------------------------------------------
    Spec("helpdesk_email", LINE, CONTACT, "Helpdesk email", lambda: "", optional=True,
         help="Where \"Book a demo\" and \"Contact us\" enquiries from the public site are emailed (replies go "
              "straight to the sender). Every enquiry is also kept under Portal > Enquiries. Sending uses the "
              "Email (SMTP) settings below."),
    Spec("show_helpdesk_email", BOOL, CONTACT, "Show the helpdesk email on the contact page", lambda: False,
         help="Also show the address on the contact page, for people who prefer to write directly. Public "
              "addresses attract spam."),
    # --- Email (SMTP) (global) ------------------------------------------------------------------
    Spec("smtp_host", LINE, EMAIL, "SMTP server", lambda: "", optional=True,
         help="Your mail provider's SMTP server, e.g. smtp-relay.brevo.com, smtp.sendgrid.net or smtp.gmail.com. "
              "Blank: emails are not sent, only written to the server log. Use \"Send a test email\" at the top "
              "of this page after saving."),
    Spec("smtp_port", INT, EMAIL, "Port", lambda: 587, min=1, max=65535,
         help="Usually 587 with STARTTLS, or 465 with SSL/TLS."),
    Spec("smtp_security", CHOICE, EMAIL, "Encryption", lambda: "starttls", choices=tuple(SMTP_SECURITY)),
    Spec("smtp_username", LINE, EMAIL, "Username", lambda: "", optional=True,
         help="Often your full email address, or the login your provider gives you."),
    Spec("smtp_password", SECRET, EMAIL, "Password", lambda: "",
         help="Or the app password / API key your provider gives you. Stored encrypted and never shown again."),
    Spec("email_from", LINE, EMAIL, "Send emails from", lambda: "", optional=True,
         help="The sender, e.g. \"SaaS Chat <noreply@yourcompany.com>\". Most providers only accept addresses "
              "on a domain you have verified with them. Blank: the SMTP username."),
    Spec("smtp_timeout", INT, EMAIL, "Timeout (seconds)", lambda: 15, min=1, max=120,
         help="How long to wait for the mail server before giving up."),
    # --- Traffic (global) -----------------------------------------------------------------------
    Spec("traffic_enabled", BOOL, TRAFFIC, "Record requests", lambda: True,
         help="Record each request for Traffic > Traffic dashboard: page, status, time taken, workspace, browser, "
              "device and (below) IP address. Unique visitors are counted without cookies."),
    Spec("trusted_proxies", INT, TRAFFIC, "Proxies in front of the app", lambda: 1, min=0, max=5,
         help="How many reverse proxies pass requests to the app: 1 for nginx (or Apache, Caddy) on the server, 2 "
              "with Cloudflare or a load balancer in front of that, 0 if visitors reach the app directly. Used to "
              "find visitors' real IP addresses (X-Forwarded-For) for rate limits and Traffic. If every visitor "
              "shows the same IP, such as 127.0.0.1, this is too low."),
    Spec("traffic_store_ip", BOOL, TRAFFIC, "Store IP addresses", lambda: True,
         help="IP addresses are personal data in many countries. Off: visitors are still counted, but no address "
              "is kept."),
    Spec("traffic_retention_days", INT, TRAFFIC, "Keep request logs for (days)", lambda: 90, min=1, max=3650,
         help="Older requests are deleted automatically (checked hourly)."),
    Spec("traffic_exclude_paths", TEXT, TRAFFIC, "Don't record these paths", lambda: "/static/\n/media/\n"
         "/favicon.ico\n/robots.txt", optional=True,
         help="One per line. A line matches every path that starts with it."),
]
BY_NAME = {s.name: s for s in SPECS}
WORKSPACE_SPECS = [s for s in SPECS if s.per_workspace]
SECTIONS = list(dict.fromkeys(s.section for s in SPECS))
