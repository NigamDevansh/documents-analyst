#!/usr/bin/env bash
#
# Damage the final document, restart the worker, and watch it restore the file
# from the record — without inventing a new version.
#
# The claim being demonstrated: `final-document.docx` is never the source of
# truth. `piles.deliverable_sections` records exactly what the last commit wrote,
# section by section, with a SHA-256 for each. On every run `Resolve` reads the
# file back and compares; a file that is missing, truncated (a crash mid-copy, a
# sync tool) or edited by hand is rewritten from the record before anything new
# is committed on top. The version number does not move, because restoring is
# not committing.
#
# The crash *inside* a commit — file written, record not yet committed — is
# covered by `test_a_crash_after_writing_the_file_loses_nothing_and_duplicates_nothing`:
# the transaction rolls back and the next run records the same sections once.

set -euo pipefail

DB="${DATABASE_URL:-postgresql+psycopg://analyst:analyst@localhost:5432/documents_analyst}"
PILE="${1:-acme}"
DOC="watched/${PILE}/final-document.docx"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

report() {
  uv run python - "$DB" "$PILE" "$DOC" <<'PY'
import sys
from pathlib import Path

import sqlalchemy as sa

from documents_analyst import deliverable

engine = sa.create_engine(sys.argv[1], future=True)
with engine.connect() as conn:
    row = conn.execute(
        sa.text(
            "SELECT id, status, deliverable_version, deliverable_sha256, deliverable_sections "
            "FROM piles WHERE name = :n"
        ),
        {"n": sys.argv[2]},
    ).first()
if row is None:
    sys.exit(f"no pile named {sys.argv[2]!r} — run `make seed && make run` first")
if not row.deliverable_version:
    sys.exit("no final document committed yet — approve the review queue and let the run finish")
sections = deliverable.recorded_sections(row.deliverable_sections)
check = deliverable.verify(Path(sys.argv[3]), sections)
print(f"   pile      {row.id}  ({row.status})")
print(f"   version   {row.deliverable_version}")
print(f"   sha256    {row.deliverable_sha256}")
print(f"   file      {sys.argv[3]}")
print(f"   verified  {'yes — ' + str(check.sections_checked) + ' sections match' if check.ok else 'NO — ' + check.summary()}")
PY
}

say "1. The document as recorded"
report

say "2. Damaging the file on disk (truncated, as a crash mid-copy would leave it)"
head -c 200 "$DOC" > "$DOC.partial" && mv "$DOC.partial" "$DOC"
uv run python - "$DB" "$PILE" <<'PY'
import sys
import sqlalchemy as sa

engine = sa.create_engine(sys.argv[1], future=True)
with engine.begin() as conn:
    pile = conn.execute(sa.text("SELECT id FROM piles WHERE name = :n"), {"n": sys.argv[2]}).one()
    # Make the pile claimable again, exactly as a restarted worker would find it.
    conn.execute(
        sa.text("UPDATE piles SET status = 'idle', lease_expires_at = NULL WHERE id = :p"),
        {"p": pile.id},
    )
    conn.execute(
        sa.text("UPDATE documents SET status = 'ingested' WHERE pile_id = :p AND status = 'extracted'"),
        {"p": pile.id},
    )
PY
report || true

say "3. Restarting the worker — Resolve runs first and restores the file"
docker compose --profile full run --rm worker \
  python -m documents_analyst.worker.main --once 2>&1 |
  grep -E "needs repair|run [0-9a-f]{8}|commit (wrote|left)" ||
  true

say "4. After"
report

cat <<'EOF'

   Same version, same SHA-256, and the file reads back section for section.
   The timeline shows "Restored the final document from the record".

EOF
