"""Migrations and `tables.py` must describe the same schema.

Nothing generates one from the other: migrations are hand-written SQL,
`tables.py` is hand-written Core metadata. So they can silently diverge, and
the symptom would be a runtime error in whichever query touched the drifted
column — usually far from the edit that caused it.

This applies every migration to a **scratch database**, reflects the result,
and compares. A silent runtime bug becomes a red test.

Marked `db`: skipped when no Postgres is reachable, so the rest of the suite
still runs offline.

    make up                       # or any Postgres with pgvector
    uv run pytest -m db
"""

from __future__ import annotations

import contextlib
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

from documents_analyst.db import migrate
from documents_analyst.db.tables import TABLE_COUNT, metadata

pytestmark = pytest.mark.db

SCRATCH = "analyst_schema_drift_test"


def admin_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    return url.rsplit("/", 1)[0] + "/postgres"


def base_url() -> str:
    return os.environ["DATABASE_URL"].rsplit("/", 1)[0]


@pytest.fixture(scope="module")
def scratch_engine() -> Engine:
    """A throwaway database with every migration applied.

    Built fresh rather than reusing the dev database, so the test proves the
    migrations work **from nothing** — which is what a reviewer's first
    `docker compose up` actually does.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — needs a live Postgres")

    admin = create_engine(admin_url(), isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
            conn.execute(text(f'CREATE DATABASE "{SCRATCH}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot reach Postgres: {exc}")

    url = f"{base_url()}/{SCRATCH}"
    migrate.run(url)
    engine = create_engine(url, future=True)
    yield engine
    # Dispose first, then terminate whatever is left. The pool's own close
    # can race the termination and surface an AdminShutdown from deep inside
    # psycopg — harmless, but it prints a traceback that looks like a failure.
    with contextlib.suppress(Exception):
        engine.dispose()
    with admin.connect() as conn:
        # Any still-open session blocks the drop. Terminate them first so a
        # failed test cannot leave a scratch database behind for the next run.
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ),
            {"d": SCRATCH},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
    admin.dispose()


def compile_pg(sa_type) -> str:
    """Compile a SQLAlchemy type to its Postgres spelling.

    Applied to BOTH sides. Comparing `str(type)` fails on pairs that are
    genuinely identical — `DateTime(timezone=True)` prints as "DATETIME"
    declared and "TIMESTAMP" reflected, while both compile to
    "TIMESTAMP WITH TIME ZONE".
    """
    try:
        return str(sa_type.compile(dialect=postgresql.dialect())).upper()
    except Exception:  # noqa: BLE001 — unknown types compare by name
        return str(sa_type).upper()


# ── the tests ─────────────────────────────────────────────────────────────


def test_migrations_apply_from_nothing(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            ).all()
        ]
    assert versions == [p.stem for p in migrate.discover()]


def test_exactly_the_declared_tables_exist(scratch_engine: Engine) -> None:
    """Fourteen tables, all declared in `tables.py` and created by the migrations."""
    reflected = {t for t in inspect(scratch_engine).get_table_names() if not t.startswith("pg_")}
    declared = set(metadata.tables)

    assert not declared - reflected, f"in tables.py but not created: {sorted(declared - reflected)}"
    assert not reflected - declared, f"created but not in tables.py: {sorted(reflected - declared)}"
    assert len(declared) == TABLE_COUNT


@pytest.mark.parametrize("table_name", sorted(metadata.tables))
def test_columns_match(scratch_engine: Engine, table_name: str) -> None:
    inspector = inspect(scratch_engine)
    reflected = {c["name"]: c for c in inspector.get_columns(table_name)}
    declared = metadata.tables[table_name].columns

    missing = {c.name for c in declared} - set(reflected)
    extra = set(reflected) - {c.name for c in declared}
    assert not missing, f"{table_name}: in tables.py but not in SQL: {sorted(missing)}"
    assert not extra, f"{table_name}: in SQL but not in tables.py: {sorted(extra)}"

    for col in declared:
        got = reflected[col.name]
        declared_pg = compile_pg(col.type)
        reflected_pg = compile_pg(got["type"])
        assert declared_pg == reflected_pg, (
            f"{table_name}.{col.name}: tables.py compiles to {declared_pg}, SQL made {reflected_pg}"
        )
        assert col.nullable == got["nullable"], (
            f"{table_name}.{col.name}: nullable mismatch — "
            f"tables.py {col.nullable}, SQL {got['nullable']}"
        )


def test_agreement_identity_constraint_matches_nulls_not_distinct(
    scratch_engine: Engine,
) -> None:
    """Most agreements have no external ref, so ordinary NULLS DISTINCT is wrong.

    Columns-only drift tests miss this: both constraints expose the same
    columns, but the default Postgres behaviour permits unlimited duplicate
    NULL external references and silently splits one agreement into many.

    `family` joined the key in `017`, and it needs the same treatment for the
    same reason: NULL there means "this document does not stand on its own", and
    under NULLS DISTINCT every such document would start its own agreement —
    the exact failure `010` fixed, reintroduced one column along.
    """
    reflected = next(
        constraint
        for constraint in inspect(scratch_engine).get_unique_constraints("agreements")
        if constraint["name"] == "agreements_identity_key"
    )
    declared = next(
        constraint
        for constraint in metadata.tables["agreements"].constraints
        if getattr(constraint, "name", None) == "agreements_identity_key"
    )

    expected_columns = ["pile_id", "parties_normalized", "external_ref", "family"]
    assert reflected["column_names"] == expected_columns
    assert [column.name for column in declared.columns] == expected_columns
    assert reflected.get("dialect_options", {}).get("postgresql_nulls_not_distinct") is True
    assert declared.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_claim_slot_identity_matches_the_open_subject_schema(scratch_engine: Engine) -> None:
    """A slot has exactly one subject and `NULL` must not make its key porous."""
    inspector = inspect(scratch_engine)
    reflected = next(
        constraint
        for constraint in inspector.get_unique_constraints("claim_slots")
        if constraint["name"] == "claim_slots_identity_key"
    )
    declared = next(
        constraint
        for constraint in metadata.tables["claim_slots"].constraints
        if getattr(constraint, "name", None) == "claim_slots_identity_key"
    )
    expected_columns = ["agreement_id", "field_key", "subject_free"]
    assert reflected["column_names"] == expected_columns
    assert [column.name for column in declared.columns] == expected_columns
    assert reflected.get("dialect_options", {}).get("postgresql_nulls_not_distinct") is True
    assert declared.dialect_options["postgresql"]["nulls_not_distinct"] is True

    check_names = {
        constraint["name"] for constraint in inspector.get_check_constraints("claim_slots")
    }
    assert "claim_slots_one_subject" in check_names
    assert any(
        getattr(constraint, "name", None) == "claim_slots_one_subject"
        for constraint in metadata.tables["claim_slots"].constraints
    )


def test_embedding_columns_are_384_wide(scratch_engine: Engine) -> None:
    """The width is fixed at the migration level on purpose: changing the
    embedding model is a migration, not an env var."""
    with scratch_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name, atttypmod FROM information_schema.columns c "
                "JOIN pg_attribute a ON a.attname = c.column_name "
                "JOIN pg_class cl ON cl.oid = a.attrelid AND cl.relname = c.table_name "
                "WHERE c.udt_name = 'vector'"
            )
        ).all()
    assert rows, "no vector columns found — pgvector missing?"
    for r in rows:
        assert r.atttypmod == 384, f"{r.table_name}.{r.column_name} is not vector(384)"


def test_taxonomy_seeded_with_28_closed_fields(scratch_engine: Engine) -> None:
    """The closed list is what stops the model minting `late_fee` on one run and
    `late_payment_penalty` on the next — they would never collide, and the
    contradiction would silently vanish."""
    with scratch_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM contract_fields")).scalar_one()
        groups = {
            r[0] for r in conn.execute(text("SELECT DISTINCT field_group FROM contract_fields"))
        }
    assert count == 28
    assert groups == {"identity", "commercial", "risk", "lifecycle", "service", "data"}


def test_migrations_are_idempotent(scratch_engine: Engine) -> None:
    """Re-running must be a no-op. `migrate` is a one-shot compose service that
    restarts freely, so a second run has to be safe."""
    with scratch_engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM schema_migrations")).scalar_one()
    migrate.run(scratch_engine.url.render_as_string(hide_password=False))
    with scratch_engine.connect() as conn:
        after = conn.execute(text("SELECT count(*) FROM schema_migrations")).scalar_one()
    assert before == after
