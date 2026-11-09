"""The assistant's persona: knowing when it is addressed by name, and answering
small talk (greetings, "who are you?", thanks) without documents or a model."""
import re

from django.conf import settings

_GREETING = (
    r"(hi|hello|hey|hiya|howdy|greetings|yo|good (morning|afternoon|evening|day)"
    # Nigerian Pidgin
    r"|how far|how you dey|how una dey|how body|how your side|wetin dey|wetin dey happen|how market|ehen|oya)"
    r"( there| all| everyone| o| oo| sir| ma)?"
)
_PATTERNS = [
    ("greeting", re.compile(rf"^({_GREETING})?$")),
    ("identity", re.compile(
        r"^(who are you|who is this|what are you|what is your name|what s your name|whats your name|"
        r"your name|are you|are you (a |an )?(bot|robot|human|ai|real|real person|person|machine)|"
        r"are you there|introduce yourself|tell me about yourself|is (this|that) you|who is|what is|who s|whos|"
        # Nigerian Pidgin
        r"wetin be your name|wetin dem dey call you|wetin be you|who you be|who be you|na who you be|"
        r"you be (robot|human|person|machine|bot)|na you|na you be|you dey there)( o)?$"
    )),
    ("thanks", re.compile(
        r"^((ok |okay |great |perfect |awesome |oya )?(thanks|thank you|thx|ty|cheers|tank you|tanks|i thank you|"
        r"e do|well done)( so much| very much| a lot| o| oo| jare| sir| ma)?)$"
    )),
    ("help", re.compile(
        r"^(help|what can you do|what can you help (me )?with|how can you help( me)?|what do you know( about)?|"
        r"what can i ask( you)?|what do you do|"
        r"(abeg )?wetin you fit do|how you fit help( me)?|wetin i fit ask( you)?|wetin you sabi)( o)?$"
    )),
    ("goodbye", re.compile(
        r"^(bye|goodbye|good bye|see you|see ya|that s all|thats all|bye for now|"
        r"bye bye|i dey go|make i dey go|i don go|later|see you later)( o| oo)?$"
    )),
]


def _name_pattern(name: str):
    words = [re.escape(w) for w in name.split()]
    return re.compile(r"(?<![\w])" + r"\s+".join(words) + r"(?:'s)?(?![\w])", re.IGNORECASE) if words else None


def _normalise(text: str) -> str:
    text = text.lower().replace("\u2019", "'").replace("'", " ")
    return " ".join(re.findall(r"[^\W_]+", text))


def mentions_name(text: str, name: str) -> bool:
    p = _name_pattern(name)
    return bool(p and p.search(text))


def search_text(text: str, name: str) -> str:
    """The message without the assistant's name or a leading greeting, for FAQ
    matching and document search ("Hi Tosin, how much leave?" -> "how much leave?")."""
    p = _name_pattern(name)
    cleaned = p.sub(" ", text) if p else text
    cleaned = re.sub(rf"^\s*({_GREETING})\b[\s,.!:;\-–—]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s,.!:;\-–—]+", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or text


def intent(text: str, name: str, enabled: bool = True) -> str | None:
    """'greeting', 'identity', 'thanks', 'help', 'goodbye', or None for a real question."""
    if not enabled:
        return None
    named = mentions_name(text, name)
    p = _name_pattern(name)
    rest = _normalise(p.sub(" ", text) if p else text)
    if not rest:
        return "greeting" if named else None
    if len(rest.split()) > 8:
        return None
    for label, pattern in _PATTERNS:
        if pattern.match(rest):
            if label == "identity" and rest in ("are you", "who is", "what is", "who s", "whos") and not named:
                return None  # bare fragments only count when the name was used ("Are you Tosin?")
            return label
    return None


# Words that mark a message as Nigerian Pidgin (never ordinary English words).
_PIDGIN_STRONG = {"wetin", "dey", "abeg", "una", "sabi", "wahala", "wey", "oya", "abi", "sef", "pikin", "ehen",
                  "jare", "biko", "kuku", "shey", "comot", "waka", "tanks", "dem"}
_PIDGIN_PHRASES = re.compile(r"\b(no be|na im|na so|e be like|you be|i be|i fit|you fit|e don|i don|no vex|tank you|"
                             r"how you dey|how una dey)\b")


def is_pidgin(text: str) -> bool:
    norm = _normalise(text)
    words = norm.split()
    if set(words) & _PIDGIN_STRONG or _PIDGIN_PHRASES.search(norm):
        return True
    # "How far" is a Pidgin greeting on its own ("How far Tosin"), but English in
    # "How far is the office?", so only short messages starting with it count.
    return norm.startswith("how far") and len(words) <= 3


def language(text: str) -> str:
    """'pcm' (Nigerian Pidgin) or 'en'."""
    return "pcm" if is_pidgin(text) else "en"


def reply(kind: str, cfg, lang: str = "en") -> str:
    """The small-talk reply from the resolved settings (siteconfig.conf.get/aget for the workspace)."""
    from siteconfig.conf import fill

    name = f"pidgin_{kind}_reply" if lang == "pcm" else f"{kind}_reply"
    return fill(getattr(cfg, name), cfg)


def no_answer(cfg, lang: str = "en") -> str:
    from siteconfig.conf import fill

    return fill(cfg.pidgin_no_answer_message if lang == "pcm" else cfg.no_answer_message, cfg)
