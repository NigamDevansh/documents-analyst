"""PDF / DOCX / HTML / TXT → plain text, with offsets that survive.

PORTABLE: zero internal imports. pdfplumber + stdlib — `.docx` is read as a zip
on purpose, see `extract_docx`.

`extracted_text` is the ONE canonical string Gate 1 checks against, so
re-extracting must produce identical bytes or every citation shifts. Three rules
follow, each tested: paragraphs join with a blank line (so `.md` and `.docx` of
one contract match); nothing is rewritten (no folding, collapsing or stripping);
body only (headers, footers and comments are addressable blocks in
`Structure.parts`, a better redaction handle than an offset).

See docs/CORE_INTERNALS.md § "portable/document_text.py — the canonical string".
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree


class ParseError(Exception):
    """Declared inline to keep this file copyable."""


class UnsupportedFormat(ParseError):
    pass


@dataclass(frozen=True, slots=True)
class PageSpan:
    """Where a page begins and ends in `extracted_text`, for citation display."""

    page_number: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    text: str
    pages: list[PageSpan] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)


SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"}
"""`.html` is here because it is the format real contracts arrive in — every SEC
filing, every contract saved from a browser is `.htm`. `.md` too: it is the
canonical fixture source."""

BLOCK_SEPARATOR = "\n\n"
"""One blank line between blocks, everywhere. A `.docx` has paragraphs and a
`.md` has blank-line-separated lines; they only hash alike if both render the
gap the same way."""

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _finalize(text: str) -> str:
    """Strip trailing newlines — the only rewriting this module does. A text file
    ends with a newline and a `.docx` cannot, so without this the two formats of
    one contract never share a hash. Safe because it only removes end characters:
    no surviving offset moves."""
    return text.rstrip("\n")


def extract(path: Path) -> ExtractedDocument:
    """Dispatch on suffix. Raises UnsupportedFormat for anything else."""
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise UnsupportedFormat(f"{path.name}: {suffix or 'no suffix'} is not a supported format")
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in (".html", ".htm"):
        return extract_html(path)
    if suffix == ".txt":
        return extract_txt(path)
    return extract_markdown(path)


def extract_pdf(path: Path) -> ExtractedDocument:
    """pdfplumber, chosen over AGPL-licensed PyMuPDF. A page that yields nothing
    is recorded as an empty span, not skipped: `pages` tells the caller the
    document was reached but held no text (a scanned page) rather than never
    opened."""
    import pdfplumber

    blocks: list[str] = []
    spans: list[PageSpan] = []
    cursor = 0

    try:
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                body = page.extract_text() or ""
                start = cursor
                cursor += len(body)
                spans.append(PageSpan(page_number=number, start=start, end=cursor))
                cursor += len(BLOCK_SEPARATOR)
                blocks.append(body)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 — every reader failure is one thing
        raise ParseError(f"{path.name}: could not be read as PDF — {exc}") from exc

    text = _finalize(BLOCK_SEPARATOR.join(blocks))
    # Spans were laid out against the un-stripped text; clamp so none can point
    # past the end of the string that is actually stored.
    pages = [PageSpan(s.page_number, min(s.start, len(text)), min(s.end, len(text))) for s in spans]
    return ExtractedDocument(
        text=text,
        pages=pages,
        meta={"format": "pdf", "page_count": str(len(pages))},
    )


def extract_docx(path: Path) -> ExtractedDocument:
    """Read `word/document.xml` directly rather than through python-docx, for
    two correctness reasons.

    Tracked changes: `python-docx`'s `paragraph.text` walks only direct-child
    runs, so a run inside `<w:ins>` is dropped — every insertion silently gone
    from the string citations index into. Collecting all `<w:t>` descendants
    gives the accepted-changes view for free (`<w:t>` in, `<w:delText>` out).

    Body only: headers, footers and comments are sibling parts, so opening
    `word/document.xml` alone is exactly the flow rule this module promises.
    """
    try:
        with zipfile.ZipFile(path) as package:
            xml = package.read("word/document.xml")
    except KeyError as exc:
        raise ParseError(f"{path.name}: not a Word document — no word/document.xml") from exc
    except zipfile.BadZipFile as exc:
        raise ParseError(f"{path.name}: not a readable .docx — {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"{path.name}: could not be read as DOCX — {exc}") from exc

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ParseError(f"{path.name}: word/document.xml is malformed — {exc}") from exc

    body = root.find(f"{_W}body")
    if body is None:
        raise ParseError(f"{path.name}: word/document.xml has no body")

    # `.iter()` is document order, so paragraphs inside tables land in place
    # rather than being appended after everything else.
    blocks = [_paragraph_text(p) for p in body.iter(f"{_W}p")]
    text = _finalize(BLOCK_SEPARATOR.join(b for b in blocks if b))
    return ExtractedDocument(
        text=text,
        pages=[],
        meta={"format": "docx", "paragraph_count": str(sum(1 for b in blocks if b))},
    )


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    """One paragraph, flattened. Tabs and breaks are kept — they are content, and
    a quote spanning one would not match at its offsets without them."""
    pieces: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            pieces.append(node.text or "")
        elif node.tag == f"{_W}tab":
            pieces.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            pieces.append("\n")
    return "".join(pieces)


class _Text(HTMLParser):
    """Tags out, text in, block structure preserved. Stdlib, not a parser library,
    because this file is copied into each `_portable/` and may import nothing.
    `<script>`/`<style>` bodies are dropped, and block tags become the blank line
    every format uses, so an `.htm` and a `.docx` of one agreement hash alike."""

    _BLOCK = {
        "p", "div", "br", "tr", "li", "section", "article", "table",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def extract_html(path: Path) -> ExtractedDocument:
    """HTML → the text a reader sees. Unlike `.md`, the markup is not content —
    nobody cites a `<td>` — so stripping it is right where stripping `##` would
    be wrong. Offsets index into what this returns, stable byte for byte."""
    raw, meta = _read_text(path)
    parser = _Text()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as exc:  # noqa: BLE001 — malformed markup is a parse error
        raise ParseError(f"{path.name}: could not be read as HTML — {exc}") from exc

    text = "".join(parser.parts)
    # Collapse the blank-line runs and horizontal padding into the one shape
    # every other extractor emits.
    lines = [" ".join(line.split()) for line in text.splitlines()]
    blocks = [line for line in lines if line]
    return ExtractedDocument(
        text=_finalize(BLOCK_SEPARATOR.join(blocks)), meta={"format": "html", **meta}
    )


def extract_txt(path: Path) -> ExtractedDocument:
    text, meta = _read_text(path)
    return ExtractedDocument(text=_finalize(text), meta={"format": "txt", **meta})


def extract_markdown(path: Path) -> ExtractedDocument:
    """Markdown is read as PLAIN TEXT — markers are not stripped. Every offset
    indexes into this exact string, so stripping `##` would shift every character
    after it and invalidate citations. Byte-identical to the file also keeps the
    hash stable, so the `.md`/`.docx` alias pair resolves to one document."""
    text, meta = _read_text(path)
    return ExtractedDocument(text=_finalize(text), meta={"format": "markdown", **meta})


def _read_text(path: Path) -> tuple[str, dict[str, str]]:
    """UTF-8, then Latin-1 as the fallback that cannot fail — and it says so.

    Latin-1 decodes any bytes without error, so it silently succeeds on files it
    gets wrong (Windows-1252 smart quotes become C1 control characters), leaving
    a plausible-looking quote that is wrong in the register. So the decision is
    returned: `meta['encoding']` is always set and `meta['decode_warning']`
    appears only on the fallback, for ingest to raise. Newlines normalise to
    `\\n`, so a CRLF file and its LF twin share offsets.
    """
    raw = path.read_bytes()
    meta: dict[str, str] = {}
    try:
        text = raw.decode("utf-8")
        meta["encoding"] = "utf-8"
    except UnicodeDecodeError as exc:
        text = raw.decode("latin-1")
        meta["encoding"] = "latin-1"
        meta["decode_warning"] = (
            f"not valid UTF-8 at byte {exc.start} — decoded as latin-1. "
            "If the source was Windows-1252, smart quotes and dashes are now "
            "control characters in the stored text."
        )
    return text.replace("\r\n", "\n").replace("\r", "\n"), meta
