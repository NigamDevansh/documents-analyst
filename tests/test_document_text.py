"""Extraction invariants — Phase 2A.1.

`extracted_text` is the one string every stored offset indexes into. These
tests protect the properties that make that safe, using the real fixtures
rather than synthetic strings: a citation that breaks does so on a contract,
not on a unit-test fake.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from documents_analyst_core.portable.document_text import (
    BLOCK_SEPARATOR,
    ParseError,
    UnsupportedFormat,
    extract,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
EDGE_CASES = FIXTURES / "docx_edge_cases"
"""Synthetic .docx files that hide text where a naive reader would pick it up:
tracked insertions and deletions, comments, and a header."""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── the format-alias guarantee ────────────────────────────────────────────


def test_md_and_docx_of_one_contract_produce_the_same_text() -> None:
    """`text_sha256` is what makes two files one document.

    The `.docx` fixtures are generated from the `.md` with heading styles and
    no text edits, so if these ever diverge, either extraction started
    rewriting something or the generator did.
    """
    md = extract(FIXTURES / "conflict_pile" / "01_msa.md")
    docx = extract(FIXTURES / "conflict_pile" / "01_msa.docx")

    assert md.text == docx.text
    assert sha(md.text) == sha(docx.text)


def test_markdown_markers_are_never_stripped() -> None:
    """Stripping `##` would shift every character after it and invalidate every
    citation past that point — silently, since the text still reads fine."""
    source = FIXTURES / "conflict_pile" / "01_msa.md"
    assert extract(source).text == source.read_text().rstrip("\n")


def test_extraction_is_deterministic() -> None:
    """Re-extracting must be byte-identical. If it is not, offsets stored on one
    run point somewhere else on the next."""
    path = FIXTURES / "conflict_pile" / "01_msa.docx"
    assert extract(path).text == extract(path).text


# ── docx specifics ────────────────────────────────────────────────────────


def test_tracked_insertions_survive_where_python_docx_drops_them() -> None:
    """The reason this module reads XML instead of using `python-docx`.

    `paragraph.text` collects only runs that are direct children of the
    paragraph. A run inside `<w:ins>` is a grandchild, so python-docx returns
    the document *without* the insertions — no error, no warning, just missing
    text in the one string every citation indexes into.
    """
    docx = pytest.importorskip("docx")
    path = EDGE_CASES / "tracked_changes_and_comments.docx"

    ours = extract(path).text
    theirs = "\n\n".join(p.text for p in docx.Document(str(path)).paragraphs if p.text)

    assert "INSERTEDNAME" in ours, "inserted text must be present"
    assert "INSERTEDNAME" not in theirs, (
        "python-docx now keeps insertions — re-check whether this module still "
        "needs to read the XML itself"
    )


def test_deleted_text_is_absent() -> None:
    """Accepted-changes view: `<w:delText>` is not `<w:t>`, so deletions fall
    out without special-casing.

    The string checked here is really in this fixture's `<w:delText>` — an
    earlier version of this test asserted a name that appears nowhere, which
    passes whatever the code does.
    """
    path = EDGE_CASES / "tracked_changes_and_comments.docx"
    with zipfile.ZipFile(path) as package:
        xml = package.read("word/document.xml").decode("utf-8", "ignore")
    assert "<w:delText" in xml and "DELETEDNAME" in xml, "fixture no longer has a deletion"

    assert "DELETEDNAME" not in extract(path).text


def test_headers_and_footers_stay_out_of_the_flow() -> None:
    """Body only. The header in this fixture really does carry a marker, which a
    caller reaches through `Structure.parts` — an addressable block — rather than
    through an offset into this string.
    """
    path = EDGE_CASES / "confidential_header.docx"
    with zipfile.ZipFile(path) as package:
        assert "CONFIDENTIAL" in package.read("word/header1.xml").decode("utf-8", "ignore")

    assert "CONFIDENTIAL" not in extract(path).text


def test_comments_stay_out_of_the_flow() -> None:
    """Same rule, different hiding place. `comments.xml` in this fixture holds a
    name and a card number; neither belongs in the string a citation offsets
    into, and both are reachable as `Structure.parts` blocks."""
    path = EDGE_CASES / "tracked_changes_and_comments.docx"
    with zipfile.ZipFile(path) as package:
        assert "COMMENTNAME" in package.read("word/comments.xml").decode("utf-8", "ignore")

    assert "COMMENTNAME" not in extract(path).text


def test_blocks_are_separated_by_one_blank_line() -> None:
    text = extract(FIXTURES / "conflict_pile" / "01_msa.docx").text
    assert BLOCK_SEPARATOR in text
    assert "\n\n\n" not in text, "an empty paragraph would double the separator"


# ── pdf ───────────────────────────────────────────────────────────────────


def test_pdf_records_a_span_for_every_page() -> None:
    doc = extract(FIXTURES / "conflict_pile" / "02_sow.pdf")
    assert doc.pages
    for span in doc.pages:
        assert 0 <= span.start <= span.end <= len(doc.text)
    assert doc.meta["page_count"] == str(len(doc.pages))


def test_page_spans_index_into_the_stored_text() -> None:
    """A span that points past the end would make the source pane highlight
    nothing, or crash it."""
    doc = extract(FIXTURES / "conflict_pile" / "02_sow.pdf")
    first = doc.pages[0]
    assert doc.text[first.start : first.end].strip()


# ── encoding ──────────────────────────────────────────────────────────────


def test_clean_utf8_records_its_encoding_and_no_warning() -> None:
    doc = extract(FIXTURES / "conflict_pile" / "01_msa.md")
    assert doc.meta["encoding"] == "utf-8"
    assert "decode_warning" not in doc.meta


def test_windows_1252_is_decoded_and_says_so(tmp_path: Path) -> None:
    """Latin-1 decodes ANY byte sequence without error, so the fallback
    succeeds even on files it gets wrong.

    Windows-1252 smart quotes (0x93/0x94) and the em dash (0x97) sit in the C1
    range, which latin-1 maps to control characters. The text still reads
    plausibly, so a quote containing one would be stored, cited, and verified
    against itself — wrong in the register with nothing to say so. The warning
    is the audit signal that makes it visible.
    """
    source = tmp_path / "smart.txt"
    # 0x93 “ · 0x94 ” · 0x97 — in Windows-1252. Invalid UTF-8, valid latin-1.
    source.write_bytes(b"Fees are due \x93Net 30\x94 \x97 payable monthly.")

    doc = extract(source)

    assert doc.meta["encoding"] == "latin-1"
    assert "decode_warning" in doc.meta
    assert "Windows-1252" in doc.meta["decode_warning"]
    assert "\x93" in doc.text, "the mojibake is real — that is why it is flagged"


def test_the_fallback_never_refuses_a_readable_file(tmp_path: Path) -> None:
    """Quarantining a document we can read imperfectly loses more than it
    saves. The warning downgrades trust; it does not drop the file."""
    source = tmp_path / "odd.txt"
    source.write_bytes(b"\xff\xfe plain enough text")

    assert extract(source).text.strip()


def test_crlf_and_lf_twins_extract_identically(tmp_path: Path) -> None:
    """Offsets must not depend on which editor last saved the file."""
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(b"Clause one.\n\nClause two.\n")
    crlf.write_bytes(b"Clause one.\r\n\r\nClause two.\r\n")

    assert extract(lf).text == extract(crlf).text


# ── refusals ──────────────────────────────────────────────────────────────


def test_a_corrupt_pdf_raises_rather_than_returning_empty_text() -> None:
    """Empty text would be indexed as a document with no facts. A raised
    ParseError is what puts the file in quarantine instead."""
    with pytest.raises(ParseError):
        extract(FIXTURES / "conflict_pile" / "08_corrupt.pdf")


def test_unsupported_suffix_is_refused_by_name() -> None:
    with pytest.raises(UnsupportedFormat, match="yaml"):
        extract(FIXTURES / "conflict_pile" / "rulebook.yaml")


def test_every_supported_fixture_extracts(tmp_path: Path) -> None:
    """Sweep the pile fixtures. Anything the scanner would pick up must parse
    or raise deliberately — never return silence."""
    seen = 0
    for path in sorted(FIXTURES.glob("*_pile/*")):
        if path.suffix.lower() not in {".pdf", ".docx", ".txt", ".md"}:
            continue
        seen += 1
        if path.name == "08_corrupt.pdf":
            with pytest.raises(ParseError):
                extract(path)
            continue
        assert extract(path).text.strip(), f"{path.name} extracted to nothing"
    assert seen > 5, "fixture sweep found almost nothing — check the glob"
