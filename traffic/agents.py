"""Small user-agent parser: browser, operating system, device type and bots.

Good enough for a traffic overview; it doesn't try to identify every browser version.
Order matters: Edge and Opera say "Chrome", Chrome says "Safari", and so on.
"""
import re

_BOT = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|archiver|facebookexternalhit|embedly|preview|monitor|uptime|pingdom|"
    r"headless|lighthouse|python-requests|python-urllib|httpx|aiohttp|curl/|wget/|go-http-client|java/|okhttp|"
    r"axios|node-fetch|libwww|scrapy|postman|insomnia|moodlebot|^moodle",
    re.I,
)
_BROWSERS = [
    ("Edge", re.compile(r"Edg(e|A|iOS)?/", re.I)),
    ("Opera", re.compile(r"OPR/|Opera", re.I)),
    ("Samsung Internet", re.compile(r"SamsungBrowser/", re.I)),
    ("Firefox", re.compile(r"Firefox/|FxiOS/", re.I)),
    ("Chrome", re.compile(r"Chrome/|CriOS/", re.I)),
    ("Safari", re.compile(r"Version/[\d.]+.*Safari/", re.I)),
    ("Internet Explorer", re.compile(r"MSIE |Trident/", re.I)),
]
_OS = [
    ("iOS", re.compile(r"iPhone|iPad|iPod", re.I)),
    ("Android", re.compile(r"Android", re.I)),
    ("Windows", re.compile(r"Windows", re.I)),
    ("ChromeOS", re.compile(r"CrOS", re.I)),
    ("macOS", re.compile(r"Mac OS X|Macintosh", re.I)),
    ("Linux", re.compile(r"Linux|X11", re.I)),
]


def parse(user_agent: str) -> dict:
    """{"browser", "os", "device", "is_bot"}; "device" is desktop, mobile, tablet, bot or other."""
    ua = user_agent or ""
    if not ua.strip():
        return {"browser": "Unknown", "os": "Unknown", "device": "other", "is_bot": False}
    if _BOT.search(ua):
        name = re.search(r"([A-Za-z][\w.-]*?(?:bot|crawler|spider))", ua, re.I)
        tool = re.match(r"([\w.-]+)/", ua)
        label = (name.group(1) if name else tool.group(1) if tool else "Bot")[:40]
        return {"browser": label, "os": "Bot", "device": "bot", "is_bot": True}
    browser = next((n for n, rx in _BROWSERS if rx.search(ua)), "Other")
    os_name = next((n for n, rx in _OS if rx.search(ua)), "Other")
    if re.search(r"iPad|Tablet|Android(?!.*Mobile)", ua, re.I):
        device = "tablet"
    elif re.search(r"Mobi|iPhone|iPod|Android.*Mobile", ua, re.I):
        device = "mobile"
    elif os_name in ("Windows", "macOS", "Linux", "ChromeOS"):
        device = "desktop"
    else:
        device = "other"
    return {"browser": browser, "os": os_name, "device": device, "is_bot": False}
