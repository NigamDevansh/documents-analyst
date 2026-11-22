"""Review fixtures with real, live source facts behind every offered option."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.db.repo import agreements, documents, facts, slots
from documents_analyst.worker.lease import Fence
from documents_analyst_core.provenance.models import Citation, GroundedFact


def seed_options(conn: Connection, fence: Fence, options: list[dict[str, Any]]) -> str:
    agreement = agreements.ensure(conn, fence, parties=("acme", "beta", uuid.uuid4().hex))
    slot = str(slots.ensure(conn, fence, agreement_id=agreement, field_key="recurring_price")["id"])
    for option in options:
        quote = str(option.get("quote") or option.get("value_text") or "A source term")
        document = documents.insert(
            conn,
            fence,
            sha256=uuid.uuid4().hex,
            filename="source.md",
            path=f"/tmp/{uuid.uuid4()}.md",
            stat={},
            extracted_text=quote,
            status="extracted",
        )
        agreements.attach_document(conn, fence, document, agreement)
        facts.replace_for_document(
            conn,
            fence,
            document_id=document,
            agreement_id=agreement,
            facts=[
                GroundedFact(
                    field_key="recurring_price",
                    value_text=str(option.get("value_text") or quote),
                    value_normalized=None,
                    confidence=1,
                    citation=Citation(
                        document_id=document, chunk_id=None, start=0, end=len(quote), quote=quote
                    ),
                )
            ],
        )
        fact_id = str(
            conn.execute(
                text("SELECT id FROM facts_and_claims WHERE source_document_id = :id"),
                {"id": document},
            ).scalar_one()
        )
        slots.attach_facts(conn, fence, slot, {str(option["value_group"]): [fact_id]})
        option["sources"] = [
            {
                "document_id": document,
                "filename": "source.md",
                "quote": quote,
                "char_start": 0,
                "char_end": len(quote),
            }
        ]
    return slot
