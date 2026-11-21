#!/usr/bin/env python3
"""Build the `.docx` fixtures from their canonical `.md` sources.

Why this exists
---------------
The `.docx` fixtures were originally written with every paragraph styled
`Normal`, so a reader looking for real Word heading styles found none — and
mapping register citations to sections would have been debugged against a
fixture defect rather than a real bug.

The one hard rule
-----------------
**Text is never modified — only paragraph styles.** `fixtures/ANSWER_KEY.json`
asserts 107 verbatim quotes against the `.md`, and the format-alias behaviour
requires `.md` and `.docx` to produce identical extracted text. This script
verifies both after writing, and refuses to leave a file that drifted.

    uv run python scripts/build_fixture_docs.py
    uv run python scripts/build_fixture_docs.py --check   # verify only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parent.parent

# Which .docx exist, and the .md each is built from. Deliberately explicit:
# these pairs are the format-alias test cases, so they are worth reading.
PAIRS: list[tuple[str, str]] = [
    ("fixtures/clean_pile/02_sow.md", "fixtures/clean_pile/02_sow.docx"),
    ("fixtures/conflict_pile/01_msa.md", "fixtures/conflict_pile/01_msa.docx"),
    (
        "fixtures/injection_pile/01_msa_with_injection.md",
        "fixtures/injection_pile/01_msa_with_injection.docx",
    ),
]

# A numbered clause heading: "1. Parties and Agreement", "12. Governing Law".
SECTION = re.compile(r"^\d+\.\s+\S")
# A document title: short, all caps, no sentence punctuation. "INVOICE".
TITLE = re.compile(r"^[A-Z][A-Z0-9 &/'-]{2,60}$")


def classify(line: str) -> str:
    """`Heading 1`, `Heading 2`, or `Normal`. Never touches the text itself."""
    s = line.strip()
    if len(s) > 80:
        return "Normal"
    if TITLE.match(s) and not s.endswith("."):
        return "Heading 1"
    if SECTION.match(s) and not s.endswith("."):
        return "Heading 2"
    return "Normal"


def build(md_path: Path, docx_path: Path) -> tuple[int, int]:
    doc = Document()
    headings = 0
    body = 0
    for raw in md_path.read_text().splitlines():
        if not raw.strip():
            continue
        style = classify(raw)
        # `raw.strip()` only — never rewrap, never re-punctuate, never
        # normalise. A single changed character breaks a verbatim quote.
        doc.add_paragraph(raw.strip(), style=style)
        if style.startswith("Heading"):
            headings += 1
        else:
            body += 1
    doc.save(docx_path)
    return headings, body


def extracted(docx_path: Path) -> list[str]:
    return [p.text.strip() for p in Document(docx_path).paragraphs if p.text.strip()]


def canonical(md_path: Path) -> list[str]:
    return [ln.strip() for ln in md_path.read_text().splitlines() if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify without writing")
    args = ap.parse_args()

    problems: list[str] = []
    for md_rel, docx_rel in PAIRS:
        md, dx = ROOT / md_rel, ROOT / docx_rel
        if not md.exists():
            problems.append(f"{md_rel}: missing canonical source")
            continue

        if not args.check:
            h, b = build(md, dx)
            print(f"{docx_rel}\n    {h} headings · {b} body paragraphs")

        if not dx.exists():
            problems.append(f"{docx_rel}: missing")
            continue

        want, got = canonical(md), extracted(dx)
        if want != got:
            problems.append(
                f"{docx_rel}: TEXT DRIFT — {len(want)} canonical vs {len(got)} extracted lines"
            )
            for i, (a, b_) in enumerate(zip(want, got, strict=False)):
                if a != b_:
                    problems.append(f"    line {i}: {a[:60]!r} != {b_[:60]!r}")
                    break
        else:
            styles = [p.style.name for p in Document(dx).paragraphs if p.text.strip()]
            n_head = sum(1 for s in styles if s.startswith("Heading"))
            status = "ok" if n_head else "NO HEADINGS"
            print(f"    text identical to .md ({len(got)} lines) · {n_head} headings · {status}")
            if not n_head:
                problems.append(f"{docx_rel}: no heading styles applied")

    if problems:
        print("\nProblems:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print("\nAll .docx fixtures match their .md source, with heading styles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
