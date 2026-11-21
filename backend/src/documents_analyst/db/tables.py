"""SQLAlchemy Core `Table` objects on one shared `MetaData`.

This does not create anything. Migrations are raw SQL; these objects exist so queries
are composable and parameter-bound. So the two CAN drift — hand-written metadata on
one side, hand-written SQL on the other — which `tests/test_schema_drift.py` closes:
it applies every migration to a scratch database, reflects it, and asserts the two
match. A silent runtime bug becomes a red test.

Core rather than ORM because the queries that matter aren't CRUD: `FOR UPDATE SKIP
LOCKED` and epoch-fenced updates where we need the rowcount to detect a lost lease,
which a session abstracts away.
"""

from __future__ import annotations

# pgvector's SQLAlchemy adapter gives a real `Vector` type and
# `.cosine_distance()`, instead of hand-written `<=>` casts.
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

EMBEDDING_DIM = 384
"""Fixed at the migration level, not in config — changing the width means a migration,
not an env var. It was bge-small's native size; `text-embedding-3-small` supports this
width natively too."""


def _pk() -> Column:
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


def _ts(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


# ── piles ─────────────────────────────────────────────────────────────────
piles = Table(
    "piles",
    metadata,
    _pk(),
    Column("name", Text, nullable=False, unique=True),
    Column("watch_path", Text, nullable=False),
    Column("rulebook_path", Text),
    Column("status", Text, nullable=False, server_default="idle"),
    # The generated document: where it was written, a hash over its ordered section
    # hashes, and exactly what each section said — what Resolve repairs from and
    # Reconcile verifies against.
    Column("deliverable_path", Text),
    Column("deliverable_sha256", Text),
    Column("deliverable_sections", JSONB, nullable=False, server_default="[]"),
    Column("deliverable_version", Integer, nullable=False, server_default="0"),
    Column("lease_owner_id", Text),
    Column("lease_epoch", BigInteger, nullable=False, server_default="0"),
    Column("lease_expires_at", DateTime(timezone=True)),
    _ts("created_at"),
    _ts("updated_at"),
)

# ── agreements ────────────────────────────────────────────────────────────
agreements = Table(
    "agreements",
    metadata,
    _pk(),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("title", Text),
    Column("parties_normalized", ARRAY(Text), nullable=False),
    Column("external_ref", Text),
    # The kind of contract, so an MSA and an NDA between the same two companies
    # are two agreements rather than one contradicting itself. NULL for a
    # document that does not stand on its own.
    Column("family", Text),
    _ts("created_at"),
    UniqueConstraint(
        "pile_id",
        "parties_normalized",
        "external_ref",
        "family",
        name="agreements_identity_key",
        postgresql_nulls_not_distinct=True,
    ),
)

# ── documents ─────────────────────────────────────────────────────────────
documents = Table(
    "documents",
    metadata,
    _pk(),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("agreement_id", UUID(as_uuid=True), ForeignKey("agreements.id", ondelete="SET NULL")),
    Column("sha256", Text, nullable=False),
    Column("text_sha256", Text),
    Column("filename", Text, nullable=False),
    Column("extracted_text", Text),
    Column(
        "supersedes_document_id",
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
    ),
    Column(
        "alias_of_document_id", UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    ),
    Column("paths", JSONB, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("quarantine_error", Text),
    # False when every section call failed for this document, so its fact set is
    # known-incomplete rather than known-empty.
    Column("extraction_ok", Boolean, nullable=False, server_default="true"),
    _ts("created_at"),
    _ts("updated_at"),
    UniqueConstraint("pile_id", "sha256"),
)

# ── contract_fields ───────────────────────────────────────────────────────
contract_fields = Table(
    "contract_fields",
    metadata,
    Column("field_key", Text, primary_key=True),
    Column("field_group", Text, nullable=False),
    Column("value_type", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("multi", Boolean, nullable=False, server_default="false"),
)

# ── document_chunks ───────────────────────────────────────────────────────
document_chunks = Table(
    "document_chunks",
    metadata,
    _pk(),
    Column(
        "document_id",
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("ordinal", Integer, nullable=False),
    Column("section_path", Text),
    Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False),
    Column("raw_text", Text, nullable=False),
    Column("context", Text, nullable=False, server_default=""),
    # No `embedding`, `kind` or `tsv`: a chunk
    # is a map of where a document's parts are, and the offsets are all of it.
    _ts("created_at"),
    UniqueConstraint("document_id", "ordinal"),
    CheckConstraint("char_end >= char_start"),
)

# ── facts_and_claims ──────────────────────────────────────────────────────
facts_and_claims = Table(
    "facts_and_claims",
    metadata,
    _pk(),
    Column(
        "agreement_id",
        UUID(as_uuid=True),
        ForeignKey("agreements.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("field_key", Text, ForeignKey("contract_fields.field_key")),
    Column("subject_free", Text),
    Column("value", JSONB, nullable=False),
    Column("value_norm", Text),
    Column(
        "source_document_id",
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False),
    Column("source_quote", Text, nullable=False),
    Column("fact_key_embedding", Vector(EMBEDDING_DIM)),
    # Which embedder made it. Two models can share a width and share no
    # meaning.
    Column("embedding_model", Text),
    Column("slot_id", UUID(as_uuid=True), ForeignKey("claim_slots.id", ondelete="SET NULL")),
    Column("role", Text, nullable=False, server_default="current"),
    Column("value_group", Integer),
    Column("status", Text, nullable=False, server_default="active"),
    _ts("created_at"),
)

# ── claim_slots ───────────────────────────────────────────────────────────
claim_slots = Table(
    "claim_slots",
    metadata,
    _pk(),
    Column(
        "agreement_id",
        UUID(as_uuid=True),
        ForeignKey("agreements.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("field_key", Text, ForeignKey("contract_fields.field_key")),
    Column("subject_free", Text),
    Column("state", Text, nullable=False, server_default="settled"),
    Column("current_fact_id", UUID(as_uuid=True)),
    Column("resolved_by", Text),
    Column("resolved_at", DateTime(timezone=True)),
    # Without this, the next render re-emits the taxonomy label and silently
    # undoes a rename the human just made.
    Column("label_override", Text),
    # Set when an approved orphan removal has landed. The slot keeps its facts
    # and citations and leaves the rendered register.
    Column("retired_at", DateTime(timezone=True)),
    _ts("created_at"),
    _ts("updated_at"),
    CheckConstraint(
        "(field_key IS NULL) <> (subject_free IS NULL)", name="claim_slots_one_subject"
    ),
    UniqueConstraint(
        "agreement_id",
        "field_key",
        "subject_free",
        name="claim_slots_identity_key",
        postgresql_nulls_not_distinct=True,
    ),
)

# ── findings ──────────────────────────────────────────────────────────────
findings = Table(
    "findings",
    metadata,
    _pk(),
    Column(
        "agreement_id",
        UUID(as_uuid=True),
        ForeignKey("agreements.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("rule_id", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("evaluated_by", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("citations", JSONB, nullable=False, server_default="[]"),
    Column("status", Text, nullable=False, server_default="open"),
    _ts("created_at"),
    _ts("updated_at"),
    # So a dismissal survives re-evaluation.
    UniqueConstraint("agreement_id", "rule_id"),
)

# ── pending_decisions ─────────────────────────────────────────────────────
pending_decisions = Table(
    "pending_decisions",
    metadata,
    _pk(),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("kind", Text, nullable=False),
    Column("ref_id", UUID(as_uuid=True)),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("relationship", Text),
    Column("detected_by", Text),
    Column("evidence_quote", Text),
    # Groups items describing the same clause into one review card.
    Column("group_key", Text),
    Column("recommendation", Text),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("chosen_value_group", Integer),
    Column("decided_by", Text),
    Column("decided_at", DateTime(timezone=True)),
    Column("decided_note", Text),
    _ts("created_at"),
)

# ── deliverable_commits ───────────────────────────────────────────────────
deliverable_commits = Table(
    "deliverable_commits",
    metadata,
    _pk(),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("decision_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"),
    Column("slot_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"),
    Column("version", Integer, nullable=False),
    Column("document_sha256", Text, nullable=False),
    Column("lease_epoch", BigInteger, nullable=False),
    _ts("created_at"),
)

# ── deliverable_versions ──────────────────────────────────────────────────
deliverable_versions = Table(
    "deliverable_versions",
    metadata,
    _pk(),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("version", Integer, nullable=False),
    Column(
        "changed_slot_id", UUID(as_uuid=True), ForeignKey("claim_slots.id", ondelete="SET NULL")
    ),
    Column("section_label", Text),
    Column("operation", Text),
    Column("before_text", Text),
    Column("after_text", Text),
    Column("before_sha256", Text),
    Column("after_sha256", Text),
    Column("reason", Text, nullable=False),
    Column(
        "decision_id", UUID(as_uuid=True), ForeignKey("pending_decisions.id", ondelete="SET NULL")
    ),
    _ts("created_at"),
    # No unique constraint: an append-only log with one row per section a commit
    # changed, so one version carries as many rows as it changed sections.
)

# ── stage_events ──────────────────────────────────────────────────────────
stage_events = Table(
    "stage_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("type", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    _ts("created_at"),
    UniqueConstraint("run_id", "seq"),
)

# ── stage_runs ────────────────────────────────────────────────────────────
stage_runs = Table(
    "stage_runs",
    metadata,
    _pk(),
    Column(
        "pile_id", UUID(as_uuid=True), ForeignKey("piles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("node", Text, nullable=False),
    Column("model", Text),
    Column("tokens_in", Integer, nullable=False, server_default="0"),
    Column("tokens_out", Integer, nullable=False, server_default="0"),
    Column("attempts", Integer, nullable=False, server_default="1"),
    Column("ok", Boolean, nullable=False, server_default="true"),
    Column("latency_ms", Integer),
    Column("llm_cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    _ts("created_at"),
)

# ── schema_migrations ─────────────────────────────────────────────────────
schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Text, primary_key=True),
    _ts("applied_at"),
    Column("checksum", Text, nullable=False),
)

__all__ = [
    "EMBEDDING_DIM",
    "agreements",
    "claim_slots",
    "contract_fields",
    "deliverable_commits",
    "deliverable_versions",
    "document_chunks",
    "documents",
    "facts_and_claims",
    "findings",
    "metadata",
    "pending_decisions",
    "piles",
    "schema_migrations",
    "stage_events",
    "stage_runs",
]

TABLE_COUNT = 14
"""Fourteen tables, all in `db/migrations/002_schema.sql` (plus the migrations ledger)."""
