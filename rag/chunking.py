"""Split text into overlapping chunks, preferring natural boundaries."""
import re

# Preferred split points, strongest first.
_BOUNDARIES = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Return chunks of at most ``size`` characters overlapping by about ``overlap``."""
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    text = normalise(text)
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Cut at the strongest boundary in the second half of the window.
            window = text[start:end]
            for sep in _BOUNDARIES:
                cut = window.rfind(sep, size // 2)
                if cut != -1:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        else:
            # Start the overlap on a word/line boundary rather than mid-word.
            boundary = _WHITESPACE.search(text, next_start, end)
            if boundary:
                next_start = boundary.end()
        start = next_start
    return chunks


# --- Section-aware chunking -------------------------------------------------------

# Numbered ("1. Operating Hours", "2.3 Delivery") or Markdown ("## Prices") headings.
_HEADING = re.compile(r"^(#{1,6}\s+\S.{0,88}|\d{1,2}(\.\d{1,2})*[.)]?\s+[A-Z][^\n]{2,88})$")
MIN_SECTION_CHARS = 40  # anything shorter stays with the previous section


def _is_heading(line: str) -> bool:
    line = line.strip()
    return bool(_HEADING.match(line)) and not line.endswith((".", ",", ";"))


def _number(line: str):
    """(1,) for "1. Opening Hours", (2, 3) for "2.3 Delivery", None for Markdown headings."""
    m = re.match(r"(\d{1,2}(?:\.\d{1,2})*)", line.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)] in document order; text before the first heading has heading "".

    A numbered line only starts a section when its number continues the document's
    numbering (1, 2, 3 / 2.1, 2.2): numbered lists inside a section restart at 1
    and stay part of it.
    """
    sections: list[list] = [["", []]]
    last = ()
    for line in normalise(text).split("\n"):
        if _is_heading(line):
            number = _number(line)
            if number is None or number > last:
                if number is not None:
                    last = number
                sections.append([line.strip().lstrip("#").strip(), []])
                continue
        sections[-1][1].append(line)
    merged: list[tuple[str, str]] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if merged and len(body) < MIN_SECTION_CHARS:
            prev_heading, prev_body = merged[-1]
            merged[-1] = (prev_heading, "\n".join(x for x in (prev_body, heading, body) if x))
        elif heading or body:
            merged.append((heading, body))
    return merged


def chunk_sections(text: str, size: int, overlap: int) -> list[tuple[str, str]]:
    """[(section heading, chunk)]: chunks never span two sections."""
    out = []
    for heading, body in split_sections(text):
        for piece in chunk_text(body, size, overlap):
            out.append((heading, piece))
    return out
