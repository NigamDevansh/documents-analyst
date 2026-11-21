"""Repository: chunks. A map of the document — never the source of a citation.

A chunk's boundaries fall where a window ended, not where a clause did; citations index into
`documents.extracted_text` with their own offsets so a chunk that split a sentence doesn't
become a quote that does. These rows are no longer embedded — nothing ever read the vectors —
so what survives is ordinal, section path and offsets.

Replace, never append: `UNIQUE (document_id, ordinal)` + `ON CONFLICT DO NOTHING` makes an
identical re-run a no-op, and the `DELETE` handles a re-run that produces FEWER chunks (else the
old tail survives as orphaned rows).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence

DELETE_FOR_DOCUMENT = text(
    f"DELETE FROM document_chunks WHERE document_id = :document_id AND {HOLDS_LEASE}"
)

INSERT = text(f"""
    INSERT INTO document_chunks (document_id, pile_id, ordinal, section_path,
                                 char_start, char_end, raw_text, context)
         SELECT :document_id, :pile_id, :ordinal, :section_path,
                :char_start, :char_end, :raw_text, :context
          WHERE {HOLDS_LEASE}
    ON CONFLICT (document_id, ordinal) DO NOTHING
""")
"""No `tsv` since 021 and no `embedding` since 018. Both were halves of a hybrid
retrieval query the architecture described and nobody wrote. What a chunk is for is the
pair of offsets: where in the document this part begins and ends."""


def replace_for_document(
    conn: Connection,
    fence: Fence,
    document_id: str,
    rows: list[dict[str, Any]],
) -> int:
    """Every chunk for one document, atomically swapped. Delete-then-insert inside the
    caller's transaction, so a reader never sees a document with half its chunks."""
    conn.execute(
        DELETE_FOR_DOCUMENT,
        {"document_id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )
    for row in rows:
        conn.execute(
            INSERT,
            {
                **row,
                "document_id": document_id,
                "pile_id": fence.pile_id,
                "epoch": fence.epoch,
            },
        )
    return len(rows)


def count_for_document(conn: Connection, document_id: str) -> int:
    return int(
        conn.execute(
            text("SELECT count(*) FROM document_chunks WHERE document_id = :id"),
            {"id": document_id},
        ).scalar_one()
    )
