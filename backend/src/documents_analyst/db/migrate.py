"""Apply `db/migrations/*.sql` in order, once each.

Runs as a ONE-SHOT compose service, not on API startup: with more than one worker or
a restart loop, two processes would race the same DDL. A service that runs once and
exits makes "the schema is ready" a fact both `api` and `worker` depend on via
`service_completed_successfully`.

Deliberately small. Alembic was rejected because the schema IS the story of this
system — numbered files a reviewer reads top to bottom beat generated revision chains,
and pgvector's HNSW DDL is awkward through Alembic anyway.

    python -m documents_analyst.db.migrate            # apply what is pending
    python -m documents_analyst.db.migrate --status   # show state, change nothing
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from documents_analyst.settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "db" / "migrations"

# 001 creates schema_migrations itself, so it cannot be recorded the usual way
# until after it has run. The runner handles that by creating the ledger first.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text        NOT NULL
)
"""


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Every `NNN_*.sql`, in filename order. Numeric prefixes make that the
    same as dependency order, which is why they are mandatory."""
    if not directory.is_dir():
        raise FileNotFoundError(f"no migrations directory at {directory}")
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations found in {directory}")
    return files


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def applied(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        conn.execute(text(LEDGER_DDL))
        conn.commit()
        rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).all()
    return {r.version: r.checksum for r in rows}


def apply_one(engine: Engine, path: Path) -> None:
    """One migration, one transaction. The file and its ledger row commit together,
    so a failure halfway leaves neither — never a half-applied schema recorded as
    complete."""
    sql = path.read_text()
    with engine.begin() as conn:
        conn.execute(text(sql))
        conn.execute(
            text(
                "INSERT INTO schema_migrations (version, checksum) "
                "VALUES (:v, :c) ON CONFLICT (version) DO NOTHING"
            ),
            {"v": path.stem, "c": checksum(path)},
        )


def run(database_url: str, *, status_only: bool = False) -> int:
    engine = create_engine(database_url, future=True)
    try:
        return _run(engine, status_only=status_only)
    finally:
        # Disposed explicitly: a leaked pool keeps sessions open, and an open
        # session is enough to make a later DROP DATABASE fail.
        engine.dispose()


def _run(engine: Engine, *, status_only: bool) -> int:
    done = applied(engine)
    files = discover()

    pending: list[Path] = []
    mismatches: list[Path] = []
    for path in files:
        if path.stem not in done:
            pending.append(path)
            continue
        # An edited-after-apply migration is a real hazard: the database no
        # longer matches the file a reviewer is reading. Say so loudly.
        if done[path.stem] != checksum(path):
            mismatches.append(path)
            print(
                f"  ! {path.stem}  CHECKSUM MISMATCH — applied, then edited. "
                "The database no longer matches this file.",
                file=sys.stderr,
            )

    # `--status` is the tool you reach for BECAUSE something is wrong, so it reports
    # the full picture and signals the problem in its exit code.
    if status_only:
        for path in files:
            if path in mismatches:
                mark = "DRIFTED"
            else:
                mark = "applied" if path.stem in done else "PENDING"
            print(f"  {mark:8} {path.name}")
        print(f"\n{len(done)} applied, {len(pending)} pending, {len(mismatches)} drifted")
        return 2 if mismatches else 0

    # Compose uses this as a readiness gate, so a successful exit must mean the
    # database and the reviewed migration files agree — never apply later migrations
    # on top of a history known to have drifted.
    if mismatches:
        print(
            "Migration history is unsafe: restore the applied file or reconcile "
            "the database deliberately before continuing.",
            file=sys.stderr,
        )
        return 2

    if not pending:
        print(f"Schema up to date — {len(done)} migrations applied.")
        return 0

    for path in pending:
        print(f"  applying  {path.name}")
        apply_one(engine, path)
    print(f"\nApplied {len(pending)} migration(s). Schema is ready.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="report state, change nothing")
    ap.add_argument("--database-url", default=None, help="overrides DATABASE_URL")
    args = ap.parse_args()
    url = args.database_url or get_settings().database_url
    return run(url, status_only=args.status)


if __name__ == "__main__":
    raise SystemExit(main())
