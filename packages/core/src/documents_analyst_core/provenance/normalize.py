"""Normalisation for comparison only — never for display.

The register always shows what the contract said. These values exist so that
"thirty (30) days" and "30 days" can be recognised as the same term, and
"$10,000" and "USD 10000.00" as the same amount. Showing a normalised value
back to a lawyer would be quietly rewriting their document.

Pure functions, no models, fully testable offline.

**Every one of them returns None rather than guessing.** A field that fails to
normalise still appears in the register with its verbatim text; it just does
not take part in numeric comparison. A field that normalises *wrongly* produces
a confident, false contradiction — or hides a real one. The asymmetry is the
whole design.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

# ── durations ─────────────────────────────────────────────────────────────

WORD_NUMBERS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "eighteen": 18, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90,
}  # fmt: skip

_DAYS_PER = {"day": 1, "week": 7, "month": 30, "year": 365}
_MONTHS_PER = {"month": 1, "quarter": 3, "year": 12}

_DURATION = re.compile(
    r"(?P<value>\d+(?:,\d{3})*|[a-z]+(?:-[a-z]+)?)\s*"
    r"(?:\((?P<paren>\d+)\)\s*)?"
    r"(?P<unit>day|week|month|quarter|year|hour|business day)s?\b",
    re.IGNORECASE,
)

_NET_TERMS = re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE)


def _number(token: str, paren: str | None) -> int | None:
    """Prefer the parenthesised digits: "thirty (30) days" is the common form,
    and the two halves can disagree in a badly drafted contract. The digits are
    what a court reads and what a person checking our work will see."""
    if paren:
        return int(paren)
    cleaned = token.replace(",", "")
    if cleaned.isdigit():
        return int(cleaned)
    return WORD_NUMBERS.get(token.lower())


def normalize_duration_days(text: str) -> int | None:
    """ "thirty (30) days" → 30. "2 years" → 730. None if not a duration."""
    net = _NET_TERMS.search(text)
    if net:
        # "Net 30" carries no unit at all, and it is how payment terms are
        # written more often than "30 days".
        return int(net.group(1))

    for match in _DURATION.finditer(text):
        unit = match.group("unit").lower().replace("business ", "")
        if unit not in _DAYS_PER:
            continue
        value = _number(match.group("value"), match.group("paren"))
        if value is not None:
            return value * _DAYS_PER[unit]
    return None


def normalize_duration_months(text: str) -> int | None:
    """ "thirty-six (36) months" → 36. "3 years" → 36."""
    for match in _DURATION.finditer(text):
        unit = match.group("unit").lower()
        if unit not in _MONTHS_PER:
            continue
        value = _number(match.group("value"), match.group("paren"))
        if value is not None:
            return value * _MONTHS_PER[unit]
    return None


def normalize_duration_hours(text: str) -> int | None:
    """ "four (4) hours" → 4. Also accepts "1 business day" → 8."""
    for match in _DURATION.finditer(text):
        unit = match.group("unit").lower()
        value = _number(match.group("value"), match.group("paren"))
        if value is None:
            continue
        if unit == "hour":
            return value
        if unit == "business day":
            return value * 8
    return None


# ── money ─────────────────────────────────────────────────────────────────

_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}
_CODES = ("USD", "GBP", "EUR", "CAD", "AUD")

_MONEY = re.compile(
    r"(?P<code>USD|GBP|EUR|CAD|AUD)?\s*"
    r"(?P<symbol>[$£€])?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>k|m|million|thousand)?"
    r"(?:\s*(?P<trailing>USD|GBP|EUR|CAD|AUD))?",
    re.IGNORECASE,
)

_SCALES = {"k": 1000, "thousand": 1000, "m": 1_000_000, "million": 1_000_000}


def normalize_money(text: str) -> tuple[Decimal, str] | None:
    """ "$10,000" → (Decimal("10000"), "USD"). None if not an amount.

    Currency is never assumed. "10,000" with no symbol and no code returns
    None: treating it as dollars would let a euro cap and a dollar cap compare
    as equal, and a contradiction that should have been raised would vanish.
    """
    match = _MONEY.search(text)
    if not match:
        return None

    code = match.group("code") or match.group("trailing")
    symbol = match.group("symbol")
    currency = (code or "").upper() or _SYMBOLS.get(symbol or "", "")
    if currency not in _CODES:
        return None

    try:
        amount = Decimal(match.group("amount").replace(",", ""))
    except InvalidOperation:
        return None

    scale = match.group("scale")
    if scale:
        amount *= _SCALES[scale.lower()]
    return amount, currency


# ── percent ───────────────────────────────────────────────────────────────

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", re.IGNORECASE)


def normalize_percent(text: str) -> Decimal | None:
    match = _PERCENT.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


# ── dates ─────────────────────────────────────────────────────────────────

MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}  # fmt: skip
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})

_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")


def normalize_date(text: str) -> str | None:
    """→ ISO 8601. Ambiguous DD/MM vs MM/DD returns None rather than guessing;
    a wrong termination date is worse than a missing one.
    """
    iso = _ISO.search(text)
    if iso:
        return _iso(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    dmy = _DMY.search(text)
    if dmy and dmy.group(2).lower().rstrip(".") in MONTHS:
        return _iso(int(dmy.group(3)), MONTHS[dmy.group(2).lower().rstrip(".")], int(dmy.group(1)))

    mdy = _MDY.search(text)
    if mdy and mdy.group(1).lower().rstrip(".") in MONTHS:
        return _iso(int(mdy.group(3)), MONTHS[mdy.group(1).lower().rstrip(".")], int(mdy.group(2)))

    numeric = _NUMERIC.search(text)
    if numeric:
        first, second = int(numeric.group(1)), int(numeric.group(2))
        # 03/04/2025 is 3 April in London and 4 March in New York, and nothing
        # in the text says which. Only resolvable when one number cannot be a
        # month — and a contract's dates are exactly where guessing costs most.
        if first > 12 and second <= 12:
            return _iso(_year(numeric.group(3)), second, first)
        if second > 12 and first <= 12:
            return _iso(_year(numeric.group(3)), first, second)
        return None

    return None


def _year(raw: str) -> int:
    value = int(raw)
    return value if value > 99 else 2000 + value


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# ── entities ──────────────────────────────────────────────────────────────

SUFFIXES = (
    "incorporated", "corporation", "limited", "company",
    "inc", "llc", "llp", "lp", "ltd", "plc", "gmbh", "corp", "co", "sa", "nv", "bv", "pty", "ag",
)  # fmt: skip

_DOTS = re.compile(r"\.")
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_entity_name(name: str) -> str:
    """Casefold, strip punctuation, drop corporate suffixes. Used for MATCHING
    parties across documents, never for display.

    Dots are **deleted**, not turned into spaces. Replacing them would split
    "L.L.C." into three one-letter words that no suffix rule can match, so
    "Beta Logistics LLC" and "Beta Logistics, L.L.C." would stay two companies
    and their shared agreement would be filed twice.

    Suffixes come off the END only, and repeatedly, so a company genuinely
    called "Limited Edition Media" keeps its first word.
    """
    without_dots = _DOTS.sub("", name)
    cleaned = _SPACE.sub(" ", _PUNCT.sub(" ", without_dots)).strip().casefold()
    words = cleaned.split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words) if words else cleaned
