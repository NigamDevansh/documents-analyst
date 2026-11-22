"""The final document: what it says, where it lives, writing it, and reading it back.

The document is `<watch_root>/<pile>/<deliverable_filename>` — `final-document.docx` by
default — written into the pile's own folder so it sits next to the sources it was built
from. The scanner skips exactly that name at the pile root (`worker/scanner.py`), or the
system would ingest its own output as a new source.

**The unit is the section.** One section per register row: the agreement it belongs to,
its heading, its body text. Each section has a SHA-256 over exactly those three strings,
and `piles.deliverable_sections` records every section as last written. That record is
what makes three promises checkable rather than asserted:

- *A focused update.* A commit compares the sections it is about to write against the
  recorded ones; only sections whose hash moved get a `deliverable_versions` row. A new
  source that touches one term produces one changed section and N-1 identical hashes.
- *Graceful re-entry.* The write is temp-file + `os.replace`, so a crash leaves either
  the old file or the new one, never half of one. If the file is missing or stale on
  the next run, `Resolve` rewrites it from the record.
- *Never bluffing.* `Reconcile` reads the file back off disk and compares it, section by
  section, to the record. "Committed" means the bytes on disk say what we recorded.

Rendering is deterministic: the same slots always produce the same sections in the same
order, so re-running a commit after a crash writes the same text it would have written.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from documents_analyst.register import render
from documents_analyst.register.slots import Slot
from documents_analyst.settings import Settings

AGREEMENT_STYLE = "Heading 1"
SECTION_STYLE = "Heading 2"
TITLE_STYLE = "Title"


@dataclass(frozen=True, slots=True)
class Section:
    slot_id: str
    agreement: str
    heading: str
    body: str

    @property
    def sha256(self) -> str:
        return section_sha256(self.agreement, self.heading, self.body)

    def as_record(self) -> dict[str, str]:
        return {
            "slot_id": self.slot_id,
            "agreement": self.agreement,
            "heading": self.heading,
            "body": self.body,
            "sha256": self.sha256,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Section:
        return cls(
            slot_id=str(record.get("slot_id") or ""),
            agreement=str(record.get("agreement") or ""),
            heading=str(record.get("heading") or ""),
            body=str(record.get("body") or ""),
        )


def section_sha256(agreement: str, heading: str, body: str) -> str:
    """JSON-encoded before hashing so the field boundaries are part of the input —
    ("a b", "c") and ("a", "b c") must not collide."""
    payload = json.dumps([agreement, heading, body], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def document_sha256(sections: list[Section]) -> str:
    """One value that changes if and only if some section changed, was added, removed,
    or moved."""
    joined = "\n".join(s.sha256 for s in sections)
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


def sections_for(slots: list[Slot]) -> list[Section]:
    """The document's sections, in document order: agreements in the order their first
    row appears, rows in slot order within each. The agreement heading is not decoration —
    a pile routinely holds several contracts, and without it `Payment terms` would appear
    three times with three values and nothing saying they belong to different agreements."""
    out: list[Section] = []
    for agreement_id in dict.fromkeys(s.agreement_id for s in slots):
        members = [s for s in slots if s.agreement_id == agreement_id]
        agreement = members[0].agreement_label
        out.extend(
            Section(slot_id=s.slot_id, agreement=agreement, heading=s.label, body=render.body(s))
            for s in members
        )
    return out


def path_for(settings: Settings, pile_name: str) -> Path:
    """Where this pile's document lives, resolved inside this process's own `watch_root`.

    By pile NAME, never by a stored absolute path: the worker in a container writes
    `/data/watched/acme/…` while an API on the host reads `./watched/acme/…` — the same
    file under two paths. And a stored path is data; joining a name to our own root and
    checking containment means no row can point a read anywhere else."""
    root = Path(settings.watch_root).resolve()
    path = (root / pile_name / settings.deliverable_filename).resolve()
    if root not in path.parents:
        raise ValueError(f"pile name {pile_name!r} resolves outside {root}")
    return path


def write(path: Path, *, title: str, sections: list[Section]) -> None:
    """Write the whole document atomically. Temp file in the same directory (so the
    rename never crosses a filesystem), fsync, `os.replace`, fsync the directory. A dotfile
    temp name, because the scanner ignores dotfiles — a reader racing the write never
    sees a half-written source."""
    from docx import Document  # deferred: python-docx is only needed where a file is written
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    for page_section in document.sections:
        page_section.top_margin = page_section.bottom_margin = Inches(0.8)
        page_section.left_margin = page_section.right_margin = Inches(0.9)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.12
    normal.paragraph_format.widow_control = True
    for name, size in ((TITLE_STYLE, 22), (AGREEMENT_STYLE, 15), (SECTION_STYLE, 12)):
        style = document.styles[name]
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.size = Pt(size)
        style.paragraph_format.keep_with_next = True
        for border in style.element.xpath(".//w:pBdr"):
            border.getparent().remove(border)
    props = document.core_properties
    props.title = title
    props.author = "documents-analyst"
    props.comments = "Generated from the source documents in this folder."

    document.add_paragraph(title, style=TITLE_STYLE)
    document.add_paragraph(render.PREAMBLE)
    current_agreement: str | None = None
    for section in sections:
        if section.agreement != current_agreement:
            document.add_paragraph(section.agreement, style=AGREEMENT_STYLE)
            current_agreement = section.agreement
        document.add_paragraph(section.heading, style=SECTION_STYLE)
        document.add_paragraph(section.body)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        document.save(str(tmp))
        verification = verify(tmp, sections)
        if not verification.ok:
            raise ValueError(f"Staged final document failed verification: {verification.summary()}")
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # not every filesystem supports fsync on a directory; the rename still holds
    finally:
        os.close(fd)


def read(path: Path) -> list[Section]:
    """The sections as they are on disk, parsed from paragraph styles: `Heading 1` opens an
    agreement, `Heading 2` opens a section, the paragraphs after it are its body. The title
    and preamble come before the first agreement and are skipped. `slot_id` is unknown on
    disk and comes back empty — verification compares text, which is what a reader sees."""
    from docx import Document

    document = Document(str(path))
    out: list[Section] = []
    agreement = ""
    heading: str | None = None
    body: list[str] = []

    def flush() -> None:
        if heading is not None:
            out.append(
                Section(slot_id="", agreement=agreement, heading=heading, body="\n".join(body))
            )

    for paragraph in document.paragraphs:
        style = paragraph.style.name if paragraph.style is not None else ""
        if style == AGREEMENT_STYLE:
            flush()
            heading, body = None, []
            agreement = paragraph.text
        elif style == SECTION_STYLE:
            flush()
            heading, body = paragraph.text, []
        elif heading is not None:
            body.append(paragraph.text)
    flush()
    return out


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    sections_checked: int
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "verified" if self.ok else "; ".join(self.problems[:5])


def verify(path: Path, recorded: list[Section]) -> Verification:
    """Read the file back and compare it, in order, to what we recorded writing. Text and
    order are compared; nothing else is trusted — not the file's mtime, not our own
    belief that the write succeeded."""
    if not path.is_file():
        return Verification(ok=False, sections_checked=0, problems=[f"{path.name} is missing"])
    try:
        on_disk = read(path)
    except Exception as exc:  # a corrupt or foreign file is a finding, not a crash
        return Verification(
            ok=False, sections_checked=0, problems=[f"{path.name} could not be read: {exc}"]
        )

    problems: list[str] = []
    if len(on_disk) != len(recorded):
        problems.append(f"expected {len(recorded)} sections, found {len(on_disk)}")
    for index, (want, got) in enumerate(zip(recorded, on_disk, strict=False)):
        if want.sha256 != got.sha256:
            problems.append(f"section {index + 1} “{want.heading}” differs from what was recorded")
    return Verification(ok=not problems, sections_checked=len(on_disk), problems=problems)


def recorded_sections(raw: Any) -> list[Section]:
    """`piles.deliverable_sections` → sections. Tolerates the column arriving as a JSON
    string (some drivers) or already decoded."""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return [Section.from_record(r) for r in (raw or [])]
