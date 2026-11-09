"""Plain-text extraction for uploaded documents."""
import io


class ExtractionError(Exception):
    pass


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("Encrypted PDFs are not supported")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfReadError as exc:
        raise ExtractionError(f"Could not read PDF: {exc}") from exc


def _docx(data: bytes) -> str:
    from docx import Document

    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises several unrelated types
        raise ExtractionError("Could not read Word document") from exc
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


EXTRACTORS = {"pdf": _pdf, "docx": _docx, "txt": _text, "md": _text, "csv": _text}
SUPPORTED_EXTENSIONS = tuple(EXTRACTORS)


def extract_text(data: bytes, extension: str) -> str:
    extractor = EXTRACTORS.get(extension)
    if extractor is None:
        raise ExtractionError(f"No text extractor for .{extension} files")
    return extractor(data)
