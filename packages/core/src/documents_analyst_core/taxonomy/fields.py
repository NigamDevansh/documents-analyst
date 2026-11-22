"""The field list.

One definition, two consumers: the SQL seed for `contract_fields`, and the JSON
schema that constrains extraction. They are generated from this list, so they
cannot drift — and drift here would mean the model emitting a `field_key` that
has no row to land in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ValueKind = Literal[
    "money",
    "percent",
    "date",
    "duration_days",
    "duration_months",
    "duration_hours",
    "boolean",
    "enum",
    "text",
    "party_list",
]
"""Durations are split by UNIT rather than normalised to one.

`payment_terms_days`, `warranty_period_months` and `support_response_hours` all
compare exactly within their own unit, and collapsing them into one `duration`
would force a conversion on every comparison — which is where "45 days" quietly
becomes "45 months". `boolean` and `party_list` exist because
`fixtures/ANSWER_KEY.json` asserts values of both shapes; a taxonomy that could
not express them would fail the fixture contract before any model ran.
"""


@dataclass(frozen=True, slots=True)
class FieldDef:
    key: str
    label: str
    kind: ValueKind
    description: str
    """Goes into the extraction prompt verbatim. This is the only instruction
    the model gets about what the field means, so it is written for the model,
    not for us."""
    multi: bool = False
    aliases: tuple[str, ...] = ()
    """Unambiguous names for this same field, never broader clause topics."""


GROUPS: tuple[str, ...] = ("identity", "commercial", "risk", "lifecycle", "service", "data")

FIELDS: tuple[FieldDef, ...] = (
    # ── identity ──────────────────────────────────────────────
    FieldDef(
        "parties",
        "Parties",
        "party_list",
        "The contracting parties, exactly as named in the preamble.",
        multi=True,
    ),
    FieldDef(
        "agreement_title",
        "Agreement title",
        "text",
        "The title of the agreement as printed on the document.",
    ),
    FieldDef(
        "agreement_type",
        "Agreement type",
        "enum",
        "MSA, SOW, invoice, amendment, NDA, or order form.",
    ),
    FieldDef("effective_date", "Effective date", "date", "The date the agreement takes effect."),
    FieldDef("expiry_date", "Expiry date", "date", "The date the agreement expires or terminates."),
    FieldDef(
        "governing_law",
        "Governing law",
        "text",
        "The jurisdiction whose law governs the agreement.",
    ),
    # ── commercial ────────────────────────────────────────────
    FieldDef(
        "total_value",
        "Total value",
        "money",
        "The total contract value, if stated as a single figure.",
    ),
    FieldDef(
        "recurring_price",
        "Recurring price",
        "money",
        "The recurring charge per billing period.",
        aliases=(
            "Monthly fee",
            "Monthly service fee",
            "Subscription fee",
            "Recurring fee",
            "Recurring charge",
        ),
    ),
    FieldDef(
        "billing_frequency",
        "Billing frequency",
        "enum",
        "How often the recurring charge is billed: monthly, quarterly, annually.",
    ),
    FieldDef(
        "payment_terms_days",
        "Payment terms",
        "duration_days",
        'Days allowed to pay an invoice. "Net 30" means 30.',
        aliases=(
            "Invoice payment period",
            "Invoice payment terms",
            "Payment deadline days",
        ),
    ),
    FieldDef(
        "late_fee_amount",
        "Late fee amount",
        "money",
        "A fixed late-payment charge, if stated as an amount.",
    ),
    FieldDef(
        "late_fee_rate", "Late fee rate", "percent", "A late-payment charge stated as a percentage."
    ),
    # ── risk ──────────────────────────────────────────────────
    FieldDef(
        "liability_cap_amount",
        "Liability cap",
        "money",
        "The maximum aggregate liability, as an amount.",
    ),
    FieldDef(
        "liability_cap_type",
        "Liability cap basis",
        "enum",
        "How the cap is expressed: fixed amount, multiple of fees, or uncapped.",
    ),
    FieldDef(
        "indemnity_present",
        "Indemnity",
        "boolean",
        "Whether the agreement contains an indemnity obligation.",
    ),
    FieldDef(
        "warranty_period_months",
        "Warranty period",
        "duration_months",
        "Length of the warranty period in months.",
    ),
    FieldDef(
        "insurance_required_amount",
        "Insurance required",
        "money",
        "Minimum insurance cover the agreement requires.",
    ),
    # ── lifecycle ─────────────────────────────────────────────
    FieldDef(
        "term_length_months",
        "Term length",
        "duration_months",
        "Length of the initial term in months.",
        aliases=("Initial term", "Initial term length"),
    ),
    FieldDef("auto_renew", "Auto-renew", "boolean", "Whether the agreement renews automatically."),
    FieldDef(
        "renewal_notice_days",
        "Renewal notice",
        "duration_days",
        "Days of notice required to prevent automatic renewal.",
    ),
    FieldDef(
        "termination_notice_days",
        "Termination notice",
        "duration_days",
        "Days of notice required to terminate.",
        aliases=("Termination notice period",),
    ),
    FieldDef(
        "termination_for_convenience",
        "Termination for convenience",
        "enum",
        "Which party may terminate without cause: both, customer, supplier, or neither.",
    ),
    # ── service ───────────────────────────────────────────────
    FieldDef(
        "uptime_pct",
        "Uptime commitment",
        "percent",
        "The committed service availability, as a percentage.",
    ),
    FieldDef(
        "support_response_hours",
        "Support response",
        "duration_hours",
        "Hours within which support must respond.",
    ),
    FieldDef(
        "service_credits_present",
        "Service credits",
        "boolean",
        "Whether missed service levels earn credits.",
    ),
    # ── data ──────────────────────────────────────────────────
    FieldDef(
        "dpa_present",
        "Data processing agreement",
        "boolean",
        "Whether a data processing agreement is included or referenced.",
    ),
    FieldDef(
        "subprocessors_allowed",
        "Subprocessors allowed",
        "boolean",
        "Whether the supplier may engage subprocessors.",
    ),
    FieldDef(
        "confidentiality_months",
        "Confidentiality period",
        "duration_months",
        "How long confidentiality obligations survive, in months.",
    ),
)
"""Twenty-eight fields, and the list is CLOSED.

That is what stops the model minting `late_fee` on one document and
`late_payment_penalty` on the next: two names for one term would never collide,
so the contradiction between them silently would not exist. The enum in
`taxonomy/schema.py` is generated from exactly these keys.

`db/migrations/003_taxonomy_seed.sql` seeds the same 28 rows. The SQL is not
generated from this file — migrations are hand-written and checksummed on
purpose — so `test_taxonomy.py` compares the two instead.
"""

_BY_KEY: dict[str, FieldDef] = {f.key: f for f in FIELDS}


class UnknownField(KeyError):
    """A `field_key` outside the taxonomy.

    Raised rather than returning None, so a caller cannot carry an unknown key
    forward by accident: `facts_and_claims.field_key` is a foreign key to
    `contract_fields`, and a fact naming a key with no row cannot be written.
    """


def by_key(key: str) -> FieldDef:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise UnknownField(f"{key!r} is not in the taxonomy ({len(FIELDS)} fields)") from None


def keys() -> tuple[str, ...]:
    return tuple(_BY_KEY)


def is_known(key: str) -> bool:
    return key in _BY_KEY
