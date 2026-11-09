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
