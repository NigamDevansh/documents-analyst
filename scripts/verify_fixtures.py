#!/usr/bin/env python3
"""Verify fixture integrity before the test suite ever runs.

The system's grounding gate is a literal substring check: a fact is only
trusted if its source_quote appears character-for-character in the source
text. If the ANSWER_KEY quotes don't satisfy that, every downstream test is
asserting against a lie.

This script checks the .md sources (the canonical text) with stdlib only, so
it runs on any Python without the project installed.

Usage:  python3 scripts/verify_fixtures.py
Exit:   0 = all good, 1 = problems found
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
KEY = FIXTURES / "ANSWER_KEY.json"

# Characters LLMs emit that break exact matching against plain-text sources.
LOOKALIKES = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    " ": " ",
    "…": "...",
    "′": "'",
    "″": '"',
    "ʼ": "'",
}


def normalize(s: str) -> str:
    """Fold the differences that don't change meaning but break ==."""
    s = unicodedata.normalize("NFKC", s)
    for bad, good in LOOKALIKES.items():
        s = s.replace(bad, good)
    return " ".join(s.split())


def collect_quotes(obj, pile: str, path: str = ""):
    """Yield (pile, file, field, quote) for every quote-bearing node."""
    if isinstance(obj, dict):
        quote = obj.get("source_quote") or obj.get("evidence_quote") or obj.get("quote")
        fname = obj.get("file")
        field = obj.get("field_key") or obj.get("rule_id") or obj.get("concept") or path
        if quote and fname:
            yield pile, fname, field, quote
        for k, v in obj.items():
            yield from collect_quotes(v, pile, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from collect_quotes(v, pile, f"{path}[{i}]")


def main() -> int:
    if not KEY.exists():
        print(f"FAIL  no answer key at {KEY}")
        return 1

    data = json.loads(KEY.read_text())
    cache: dict[Path, tuple[str, str]] = {}

    exact = loose = 0
    missing_file: list[str] = []
    failures: list[tuple[str, str, str, str]] = []

    for pile, body in data.items():
        for _, fname, field, quote in collect_quotes(body, pile):
            src = FIXTURES / pile / fname
            if not src.exists():
                missing_file.append(f"{pile}/{fname}")
                continue
            if src.suffix.lower() != ".md":
                continue  # binary formats verified separately
            if src not in cache:
                raw = src.read_text(encoding="utf-8", errors="replace")
                cache[src] = (raw, normalize(raw))
            raw, norm = cache[src]

            if quote in raw:
                exact += 1
            elif normalize(quote) in norm:
                loose += 1
                failures.append((pile, fname, field, quote))
            else:
                failures.append((pile, fname, field, quote))

    print(f"exact matches   : {exact}")
    print(f"whitespace-only : {loose}   (would pass after normalization)")

    hard = [
        f for f in failures if normalize(f[3]) not in cache.get(FIXTURES / f[0] / f[1], ("", ""))[1]
    ]
    print(f"NOT FOUND       : {len(hard)}")

    if missing_file:
        print("\nreferenced files that do not exist:")
        for m in sorted(set(missing_file)):
            print(f"  - {m}")

    if hard:
        print("\nquotes that are not in their source file:")
        for pile, fname, field, quote in hard[:25]:
            print(f"\n  {pile}/{fname}  [{field}]")
            print(f"    {quote[:160]}")
        if len(hard) > 25:
            print(f"\n  ... and {len(hard) - 25} more")

    ok = not hard and not missing_file
    print("\n" + ("PASS - fixtures are consistent" if ok else "FAIL - fix the items above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
