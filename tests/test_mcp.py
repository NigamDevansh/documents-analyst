"""The MCP server — Phase 6. Seven tools, and the gate an agent cannot skip.

The tools are driven against the **real** FastAPI app through
`httpx.ASGITransport`: real routing, real status codes, real headers, no socket.
That matters more here than anywhere else in the suite, because the entire claim
of this phase is that a machine caller reaches the system only over HTTP. A test
that called the tool functions against a fake would be testing the fake and
would pass just as well if somebody imported a repository into the server.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from review_helpers import seed_options
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from documents_analyst import deliverable
from documents_analyst.api.main import create_app
from documents_analyst.db import migrate
from documents_analyst.db.repo import agreements as agreement_repo
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.db.repo import slots as slot_state
from documents_analyst.mcp.api import AnalystApi, machine_actor
from documents_analyst.mcp.server import build_server
from documents_analyst.settings import Settings
from documents_analyst.worker import lease
from documents_analyst.worker.lease import Fence
from documents_analyst_core.provenance.models import Citation, GroundedFact

pytestmark = pytest.mark.db

SCRATCH = "analyst_mcp_test_" + uuid.uuid4().hex[:12]

CONTRACT = (
    "The monthly fee shall be USD 10,000 under the original agreement.\n"
    "As amended, the monthly fee shall be USD 12,000 from April.\n"
)
QUOTE = "the monthly fee shall be USD 12,000"
RECOMMENDATION = (
    "The later document explicitly replaces the monthly fee stated in the original agreement."
)


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — needs a live Postgres")

    base = url.rsplit("/", 1)[0]
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
            conn.execute(text(f'CREATE DATABASE "{SCRATCH}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot reach Postgres: {exc}")

    scratch_url = f"{base}/{SCRATCH}"
    migrate.run(scratch_url)
    made = create_engine(scratch_url, future=True)
    yield made

    made.dispose()
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ),
            {"d": SCRATCH},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE piles CASCADE"))
        conn.execute(text("TRUNCATE stage_events, stage_runs"))
    yield


def settings_for(engine: Engine, tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "database_url": engine.url.render_as_string(hide_password=False),
        "watch_root": tmp_path,
        "mcp_api_base_url": "http://analyst.test",
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
async def wire(engine: Engine, tmp_path: Path) -> AsyncIterator[Any]:
    """Build a server whose HTTP client speaks to a real app, in this process.

    The app's **real lifespan** runs — pool and event broker included — because
    a test that hand-assembled `app.state` would be testing an app that is
    wired differently from the deployed one, which is the failure this whole
    phase is trying to make impossible one level up.
    """
    async with AsyncExitStack() as stack:

        async def make(**overrides: Any) -> Any:
            settings = settings_for(engine, tmp_path, **overrides)
            app = create_app(settings)
            await stack.enter_async_context(app.router.lifespan_context(app))
            api = AnalystApi(settings, transport=httpx.ASGITransport(app=app))
            stack.push_async_callback(api.aclose)
            return build_server(settings, api), api, settings

        yield make


@pytest.fixture
async def wired(wire: Any) -> Any:
    return await wire()


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool the way a client does, and return its structured result."""
    result = await server.call_tool(name, arguments)
    assert not result.is_error, f"{name} failed: {result.content}"
    return dict(result.structured_content or {})


async def refused(server: Any, name: str, **arguments: Any) -> str:
    """Invoke a tool that must fail, and return the message the caller sees.

    The SDK turns an exception into `ToolError`, which is what reaches the
    client as a tool error — so this is the text that lands in an agent's
    context, and asserting on it is asserting on the thing that has to be
    useful.
    """
    with pytest.raises(ToolError) as raised:
        await server.call_tool(name, arguments)
    return str(raised.value)


def make_pile(engine: Engine, tmp_path: Path, name: str = "acme") -> str:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    with engine.begin() as conn:
        return pile_repo.ensure(conn, name=name, watch_path=folder)


def raise_item(
    engine: Engine,
    pile_id: str,
    kind: str = "amendment",
    *,
    quote: str | None = QUOTE,
    recommendation: str | None = RECOMMENDATION,
) -> str:
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test-worker", 90)
    assert epoch is not None
    try:
        with engine.begin() as conn:
            fence = Fence(pile_id=pile_id, epoch=epoch)
            options = [
                {
                    "value_group": 0,
                    "value_text": quote or recommendation or "Source term",
                    "quote": quote,
                }
            ]
            ref_id = (
                seed_options(conn, fence, options)
                if kind in ("conflict", "amendment")
                else str(uuid.uuid4())
            )
            item = decision_repo.raise_decision(
                conn,
                fence,
                kind=kind,
                ref_id=ref_id,
                payload={"options": options, "recommended_value_group": 0},
                evidence_quote=quote,
                recommendation=recommendation,
                relationship="amendment",
                detected_by="rule",
            )
        assert item is not None
        return item
    finally:
        with engine.begin() as conn:
            lease.release(conn, pile_id, epoch)


# ── the surface ───────────────────────────────────────────────────────────


async def test_there_are_seven_tools_and_the_reads_say_so(wired: Any) -> None:
    """Five reads and two writes, and the annotations tell a client which.

    `read_only_hint` is what lets a host offer the reads freely and confirm the
    decisions. Getting it wrong on `approve_item` would present the one
    irreversible tool in the set as safe.
    """
    server, _, _ = wired
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {
        "list_piles",
        "get_register",
        "get_review_queue",
        "get_citations",
        "approve_item",
        "reject_item",
        "get_final_document",
    }
    read_only = {
        name for name, t in tools.items() if t.annotations and t.annotations.read_only_hint
    }
    assert read_only == {
        "list_piles",
        "get_register",
        "get_review_queue",
        "get_citations",
        "get_final_document",
    }


async def test_the_server_reaches_the_system_only_over_http() -> None:
    """The architectural claim, enforced instead of asserted in prose.

    An MCP server that imported a repository would still work, and "machine
    callers go through the same gates as the browser" would silently become a
    convention that holds until someone is in a hurry. This walks the package's
    imports and fails on any route into the database.
    """
    package = Path(__file__).resolve().parent.parent / "backend/src/documents_analyst/mcp"
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.startswith(
                    ("documents_analyst.db", "documents_analyst.worker", "sqlalchemy", "psycopg")
                ):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == [], f"the MCP server must reach the API over HTTP only: {offenders}"


# ── reading ───────────────────────────────────────────────────────────────


async def test_list_piles_reports_what_is_waiting(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    raise_item(engine, pile_id)

    body = await call(server, "list_piles")

    assert body["count"] == 1
    assert body["piles"][0]["pile_id"] == pile_id
    assert body["piles"][0]["pending_decisions"] == 1


async def test_the_queue_carries_the_evidence_with_the_item(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """Because `approve_item` demands it back.

    A queue that made an agent fetch the evidence separately is a queue it
    approves without reading — the confirmation would become a second call
    nobody makes rather than a gate.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    raise_item(engine, pile_id)

    queue = await call(server, "get_review_queue", pile_id=pile_id)

    assert queue["pending"] == 1
    assert queue["items"][0]["evidence"] == QUOTE
    assert queue["items"][0]["kind"] == "amendment"


async def test_an_unknown_pile_is_a_sentence_not_a_traceback(wired: Any) -> None:
    """A tool result lands in a model's context. It has to read like an answer."""
    server, _, _ = wired
    message = await refused(server, "get_register", pile_id=str(uuid.uuid4()))
    assert "404" in message and "no pile" in message


# ── the gate ──────────────────────────────────────────────────────────────


async def test_approving_without_quoting_the_evidence_is_refused(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """The whole point of the approve tool.

    An agent looping over decision ids cannot pass this; one that read the
    conflict can. The refusal returns the real evidence, which is the mechanism
    working rather than a hole — the text ends up in the caller's context before
    any approval lands.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    message = await refused(
        server, "approve_item", decision_id=decision_id, confirm="looks fine to me"
    )

    assert QUOTE in message, "the refusal hands back the evidence the caller must read"
    with engine.begin() as conn:
        assert decision_repo.get(conn, decision_id)["status"] == "pending"  # type: ignore[index]


async def test_a_near_miss_is_still_a_miss(wired: Any, engine: Engine, tmp_path: Path) -> None:
    """Exact substring, no fuzzy matching, no threshold — the Gate 1 rule again.

    `USD 12,500` is one character from the truth and describes a different
    contract. A similarity score would let it through at almost any threshold,
    which is exactly the guess this check exists to remove.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    await refused(
        server,
        "approve_item",
        decision_id=decision_id,
        confirm="the monthly fee shall be USD 12,500",
    )


async def test_a_fragment_too_short_to_prove_reading_is_refused(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """ "USD" appears in the evidence and proves nothing."""
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    await refused(server, "approve_item", decision_id=decision_id, confirm="USD")


async def test_quoting_the_evidence_approves_it_as_a_machine(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """And `decided_by` says a machine did it."""
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    body = await call(
        server,
        "approve_item",
        decision_id=decision_id,
        confirm=QUOTE,
        note="the amendment governs",
    )

    assert body["approved"] is True
    assert body["decided_by"] == "mcp:agent"
    with engine.begin() as conn:
        stored = decision_repo.get(conn, decision_id)
    assert stored is not None
    assert (stored["status"], stored["decided_by"]) == ("approved", "mcp:agent")


async def test_whitespace_may_differ_but_nothing_else(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """A quote copied out of a tool result may have been re-wrapped.

    Normalising whitespace is the one concession; every other character must
    match, so this is still a verbatim check.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    body = await call(
        server,
        "approve_item",
        decision_id=decision_id,
        confirm="the monthly fee\n   shall be USD 12,000",
    )
    assert body["approved"] is True


async def test_a_finding_with_no_quote_falls_back_to_its_recommendation(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """A rulebook violation is the *absence* of a clause, so it has no quote.

    Demanding one would make findings unapprovable by an agent; accepting
    anything would make the gate optional for a whole decision kind.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id, "finding", quote=None)

    queue = await call(server, "get_review_queue", pile_id=pile_id)
    assert queue["items"][0]["evidence"] == RECOMMENDATION

    body = await call(server, "approve_item", decision_id=decision_id, confirm=RECOMMENDATION)
    assert body["approved"] is True


async def test_rejecting_needs_no_confirmation(wired: Any, engine: Engine, tmp_path: Path) -> None:
    """Rejecting commits nothing. Friction on the safe direction only teaches
    callers to approve."""
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    body = await call(server, "reject_item", decision_id=decision_id, note="not convinced")

    assert body["rejected"] is True
    with engine.begin() as conn:
        assert decision_repo.get(conn, decision_id)["status"] == "rejected"  # type: ignore[index]


async def test_deciding_twice_reports_who_won_rather_than_overwriting(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """The API's 409 reaches the agent as an answer it can act on."""
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)
    with engine.begin() as conn:
        decision_repo.settle(conn, decision_id, status="approved", actor="devansh")

    body = await call(server, "approve_item", decision_id=decision_id, confirm=QUOTE)

    assert body["approved"] is False
    assert "devansh" in body["reason"]


async def test_the_actor_is_always_machine_tagged(
    wire: Any, engine: Engine, tmp_path: Path
) -> None:
    """An operator writing `MCP_ACTOR=devansh` means "the agent I run".

    Taken literally it would put approvals in the trail that are
    indistinguishable from ones a person made in the UI, and telling those apart
    is the entire reason `decided_by` is not just "someone".
    """
    server, _, _ = await wire(mcp_actor="devansh")
    pile_id = make_pile(engine, tmp_path)
    decision_id = raise_item(engine, pile_id)

    body = await call(server, "approve_item", decision_id=decision_id, confirm=QUOTE)

    assert body["decided_by"] == "mcp:devansh"
    assert machine_actor("mcp:ci") == "mcp:ci"  # already tagged, not doubled
    assert machine_actor("") == "mcp:agent"


# ── authorization ─────────────────────────────────────────────────────────


async def test_a_token_is_carried_through_to_every_call(
    wire: Any, engine: Engine, tmp_path: Path
) -> None:
    server, _, _ = await wire(api_token="s3cret")
    make_pile(engine, tmp_path)
    assert (await call(server, "list_piles"))["count"] == 1


async def test_a_missing_token_says_which_variable_to_set(engine: Engine, tmp_path: Path) -> None:
    """The API is locked and the MCP process was not told.

    A bare `401` sends somebody to the wrong file. The one thing the error
    knows — that this instance wants `API_TOKEN` — belongs in it.
    """
    settings = settings_for(engine, tmp_path)  # no token for the client
    locked = create_app(settings_for(engine, tmp_path, api_token="s3cret"))
    api = AnalystApi(settings, transport=httpx.ASGITransport(app=locked))
    server = build_server(settings, api)

    message = await refused(server, "list_piles")

    assert "401" in message and "API_TOKEN" in message


# ── the final document ────────────────────────────────────────────────────


def committed_document(engine: Engine, tmp_path: Path, pile_id: str) -> Path:
    """The state `Commit` leaves behind: a final document in the pile folder and the
    record of it. Built directly — these tests are about the MCP surface, not the graph."""
    sections = [deliverable.Section("s1", "Acme ↔ Beta", "Recurring Price", "USD 12,000")]
    path = tmp_path / "acme" / "final-document.docx"
    deliverable.write(path, title="Final Document — acme", sections=sections)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE piles SET deliverable_version = 1, deliverable_path = :path, "
                "deliverable_sha256 = :sha, deliverable_sections = CAST(:sections AS jsonb) "
                "WHERE id = :p"
            ),
            {
                "p": pile_id,
                "path": str(path),
                "sha": deliverable.document_sha256(sections),
                "sections": json.dumps([x.as_record() for x in sections]),
            },
        )
    return path


async def test_the_final_document_comes_back_verified(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """Not a ticket, not a path on someone else's disk: the version, its hash, and the
    result of reading the file back — plus a URI the caller can read it through."""
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    path = committed_document(engine, tmp_path, pile_id)

    body = await call(server, "get_final_document", pile_id=pile_id)
    assert body["ready"] is True
    assert body["verified"] is True and body["problems"] == []
    assert body["filename"] == "final-document.docx"
    assert body["resource_uri"] == f"document://{pile_id}"
    assert body["download_url"].endswith(f"/piles/{pile_id}/deliverable/document")

    payload = await server.read_resource(body["resource_uri"])
    content = payload[0].content if isinstance(payload, list) else payload
    assert bytes(content) == path.read_bytes()


async def test_a_pile_with_nothing_committed_says_so(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    body = await call(server, "get_final_document", pile_id=pile_id)
    assert body["ready"] is False
    assert "nothing has been committed" in body["reason"]


async def test_a_tampered_document_is_reported_not_vouched_for(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    path = committed_document(engine, tmp_path, pile_id)
    deliverable.write(
        path,
        title="x",
        sections=[deliverable.Section("s1", "Acme ↔ Beta", "Recurring Price", "USD 1")],
    )
    body = await call(server, "get_final_document", pile_id=pile_id)
    assert body["verified"] is False
    assert body["problems"]


# ── choosing between conflicting values ───────────────────────────────────


def raise_conflict(engine: Engine, pile_id: str, kind: str = "conflict") -> tuple[str, str]:
    """A slot with two competing values, and the decision that asks about it.

    Built through the real repositories so the payload, the `value_group`s on
    the facts and the slot state are the ones the classifier actually produces.
    """
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test-worker", 90)
    assert epoch is not None
    fence = Fence(pile_id=pile_id, epoch=epoch)
    try:
        with engine.begin() as conn:
            agreement_id = agreement_repo.ensure(
                conn, fence, parties=("acme corporation", "beta logistics inc"), title="MSA"
            )
            document_id = doc_repo.insert(
                conn,
                fence,
                filename="01_msa.md",
                path="/tmp/01_msa.md",
                sha256=uuid.uuid4().hex,
                stat={"size": 10, "mtime_ns": 0, "inode": 0},
                extracted_text=CONTRACT,
                text_sha256=uuid.uuid4().hex,
                status="ingested",
            )
            conn.execute(
                text("UPDATE documents SET agreement_id = :a WHERE id = :d"),
                {"a": agreement_id, "d": document_id},
            )
            fact_repo.replace_for_document(
                conn,
                fence,
                document_id=document_id,
                agreement_id=agreement_id,
                facts=[
                    GroundedFact(
                        field_key="recurring_price",
                        value_text=text_value,
                        value_normalized=norm,
                        citation=Citation(
                            document_id=document_id,
                            chunk_id=None,
                            start=CONTRACT.index(text_value),
                            end=CONTRACT.index(text_value) + len(text_value),
                            quote=text_value,
                        ),
                        confidence=0.9,
                    )
                    for text_value, norm in (
                        ("USD 10,000", "10000 USD"),
                        ("USD 12,000", "12000 USD"),
                    )
                ],
            )
            facts = fact_repo.unmatched_for_agreement(conn, agreement_id)
            slot_id = str(
                slot_state.ensure(
                    conn, fence, agreement_id=agreement_id, field_key="recurring_price"
                )["id"]
            )
            slot_state.attach_facts(
                conn, fence, slot_id, {index: [str(f["id"])] for index, f in enumerate(facts)}
            )
            slot_state.set_state(conn, fence, slot_id, state="disputed")
            decision_id = decision_repo.raise_decision(
                conn,
                fence,
                kind=kind,
                ref_id=slot_id,
                evidence_quote=QUOTE,
                recommendation=RECOMMENDATION,
                relationship=kind,
                detected_by="rule",
                payload={
                    "field_key": "recurring_price",
                    "recommended_value_group": 1 if kind == "amendment" else None,
                    "options": [
                        {
                            "value_group": index,
                            "value_text": f["value"]["text"],
                            "value_norm": f["value_norm"],
                            "sources": [
                                {
                                    "document_id": str(f["source_document_id"]),
                                    "filename": "01_msa.md",
                                    "char_start": f["char_start"],
                                    "char_end": f["char_end"],
                                    "quote": f["source_quote"],
                                }
                            ],
                        }
                        for index, f in enumerate(facts)
                    ],
                },
            )
        assert decision_id is not None
        return slot_id, decision_id
    finally:
        with engine.begin() as conn:
            lease.release(conn, pile_id, epoch)


async def test_the_queue_shows_which_group_holds_which_value(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """An agent cannot choose between values it cannot name.

    `approve_item` takes a `value_group`, so a queue that returned only the
    competing *text* would leave an agent guessing whether `$12,000` is group 0
    or group 1 — a coin flip that writes a number into somebody's register.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    _, decision_id = raise_conflict(engine, pile_id)

    queue = await call(server, "get_review_queue", pile_id=pile_id)
    item = next(i for i in queue["items"] if i["decision_id"] == decision_id)

    assert [(o["value_group"], o["value"]) for o in item["options"]] == [
        (0, "USD 10,000"),
        (1, "USD 12,000"),
    ]
    assert item["options"][1]["said_by"] == ["01_msa.md"]
    assert item["field"] == "recurring_price"


async def test_approving_a_conflict_without_choosing_is_refused(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """An approval that names no winner resolves nothing.

    It used to be accepted: the slot stayed `disputed`, the register kept
    printing "sources disagree", and the commit that followed spent an operation
    rewriting that warning. The refusal lists the options, so the retry is
    informed rather than a guess.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    _, decision_id = raise_conflict(engine, pile_id)

    message = await refused(server, "approve_item", decision_id=decision_id, confirm=QUOTE)

    assert "chosen_value_group" in message
    assert "USD 12,000" in message
    with engine.begin() as conn:
        assert decision_repo.get(conn, decision_id)["status"] == "pending"  # type: ignore[index]


async def test_choosing_a_group_records_it(wired: Any, engine: Engine, tmp_path: Path) -> None:
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    _, decision_id = raise_conflict(engine, pile_id)

    body = await call(
        server, "approve_item", decision_id=decision_id, confirm=QUOTE, chosen_value_group=1
    )

    assert body["approved"] is True
    with engine.begin() as conn:
        assert decision_repo.get(conn, decision_id)["chosen_value_group"] == 1  # type: ignore[index]


async def test_an_amendment_may_be_approved_without_choosing(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """ "The later document supersedes" is mechanical, and the batch lane needs it.

    Requiring a group here would mean approving twelve amendments one at a time,
    which is the click-without-reading habit the queue exists to prevent.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    _, decision_id = raise_conflict(engine, pile_id, kind="amendment")

    body = await call(server, "approve_item", decision_id=decision_id, confirm=QUOTE)
    assert body["approved"] is True


async def test_an_approved_choice_reaches_the_register(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """The end of the chain, and the bug this whole fix is about.

    `chosen_value_group` used to be written by the API and read by nobody. The
    slot stayed `disputed`, `Slot.current` stayed `None`, and the register kept
    rendering "⚠ sources disagree — awaiting review" into the final document.

    So this asserts what a reader of the document would see, not that a column
    changed: after approving group 1, the register states USD 12,000.
    """
    from documents_analyst.graph.nodes import commit as commit_node
    from documents_analyst.register import render
    from documents_analyst.register import slots as register_slots

    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    slot_id, decision_id = raise_conflict(engine, pile_id)

    with engine.begin() as conn:
        before = next(s for s in register_slots.for_pile(conn, pile_id) if s.slot_id == slot_id)
    assert before.state == "disputed"
    assert render.DISPUTED in render.body(before)

    await call(server, "approve_item", decision_id=decision_id, confirm=QUOTE, chosen_value_group=1)

    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "committer", 90)
    assert epoch is not None
    with engine.begin() as conn:
        approved = decision_repo.approved_undecided(conn, pile_id)
        applied = commit_node._apply_resolutions(
            conn, Fence(pile_id=pile_id, epoch=epoch), approved
        )
        after = next(s for s in register_slots.for_pile(conn, pile_id) if s.slot_id == slot_id)

    assert applied == {slot_id: 1}
    assert after.state == "resolved"
    assert after.current is not None
    assert after.current.value_text == "USD 12,000"
    assert render.body(after).splitlines()[0] == "USD 12,000"
    assert "Reviewed choice: mcp:agent" in render.body(after)
    assert render.DISPUTED not in render.body(after)


async def test_a_group_that_is_not_on_the_list_is_refused(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """`999` used to erase every value in the slot.

    Resolution demoted all non-matching facts and *then* looked for the winner,
    so a group that was never an option superseded both real values and left the
    slot disputed with nothing to choose between — the disagreement gone and the
    numbers that caused it with it. The API now checks the choice against the
    options stored on the decision.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    slot_id, decision_id = raise_conflict(engine, pile_id)

    message = await refused(
        server, "approve_item", decision_id=decision_id, confirm=QUOTE, chosen_value_group=999
    )

    assert "not one of this item's values" in message
    assert "USD 12,000" in message
    with engine.begin() as conn:
        assert decision_repo.get(conn, decision_id)["status"] == "pending"  # type: ignore[index]


def test_resolving_to_a_missing_group_changes_nothing(engine: Engine, tmp_path: Path) -> None:
    """The second line of the same defence, below the API.

    `resolve` confirms the winner exists **before** demoting anything, so a
    choice that cannot be honoured leaves the slot exactly as it was — both
    values live, the question still asked. The first version checked afterwards
    and had already destroyed the evidence by then.
    """
    from documents_analyst.register import slots as register_slots

    pile_id = make_pile(engine, tmp_path)
    slot_id, _ = raise_conflict(engine, pile_id)

    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "committer", 90)
    assert epoch is not None
    with engine.begin() as conn:
        winner = slot_state.resolve(
            conn, Fence(pile_id=pile_id, epoch=epoch), slot_id, value_group=999, decided_by="x"
        )
        after = next(s for s in register_slots.for_pile(conn, pile_id) if s.slot_id == slot_id)

    assert winner is None
    assert after.state == "disputed"
    assert [v.value_text for v in after.values] == ["USD 10,000", "USD 12,000"], (
        "a choice that cannot be honoured must not erase the values it was choosing between"
    )


# ── the transports ────────────────────────────────────────────────────────


def test_the_compose_stdio_service_actually_starts_stdio() -> None:
    """`claude mcp add … docker compose run --rm mcp` must get a process, not a port.

    The service used to set `MCP_TRANSPORT=streamable-http`, so the documented
    command started an HTTP listener and the client waited for JSON-RPC on a
    stdout that never spoke. It fails as a hang, which is the hardest kind of
    wrong to diagnose — so the transport is in the command, and this reads it.
    """
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    )
    stdio = compose["services"]["mcp"]
    assert stdio["command"][-2:] == ["--transport", "stdio"]
    assert "MCP_TRANSPORT" not in (stdio.get("environment") or {})
    assert "ports" not in stdio, "a stdio service that publishes a port is a contradiction"

    http = compose["services"]["mcp-http"]
    assert http["command"][-2:] == ["--transport", "streamable-http"]
    assert http["ports"] == ["127.0.0.1:8765:8765"], "the MCP port must not be world-reachable"


def test_a_public_bind_without_an_inbound_token_refuses_to_start(
    engine: Engine, tmp_path: Path
) -> None:
    """The confused deputy, refused rather than warned about.

    This process holds `API_TOKEN` and presents it downstream, so an open port
    with no `MCP_TOKEN` lets anyone who reaches it approve register changes
    using our credential — and the API's own guard never sees them. Locking the
    API and leaving this open is worse than leaving both open, because it looks
    protected.
    """
    from documents_analyst.mcp.server import refuse_unauthenticated_public_bind as check

    exposed = settings_for(engine, tmp_path, mcp_host="0.0.0.0", api_token="s3cret")
    assert "MCP_TOKEN" in str(check(exposed))

    assert (
        check(
            settings_for(
                engine, tmp_path, mcp_host="0.0.0.0", api_token="s3cret", mcp_token="inbound"
            )
        )
        is None
    )
    assert check(settings_for(engine, tmp_path, mcp_host="127.0.0.1", api_token="s3cret")) is None
    # An open API behind an open port grants nothing the API does not already.
    assert check(settings_for(engine, tmp_path, mcp_host="0.0.0.0")) is None


async def test_the_http_transport_rejects_a_caller_with_no_token(
    wire: Any, engine: Engine, tmp_path: Path
) -> None:
    """`MCP_TOKEN` is what the server demands, not what it presents."""
    from documents_analyst.mcp.server import http_app

    server, _, settings = await wire(mcp_token="inbound", api_token="downstream")
    app = http_app(settings, server)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
    ) as client:
        anonymous = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert anonymous.status_code == 401
        assert "MCP_TOKEN" in anonymous.text

        wrong = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer nope"},
        )
        assert wrong.status_code == 401


async def test_the_download_url_is_one_an_outside_caller_can_reach(
    wire: Any, engine: Engine, tmp_path: Path
) -> None:
    """In compose the MCP server dials `http://api:8000`, a name only Docker
    resolves. Handing that to a host-side agent advertises a download it cannot
    follow."""
    server, _, settings = await wire(
        mcp_api_base_url="http://api:8000",
        public_api_base_url="http://localhost:8000",
    )
    assert settings.public_api_url == "http://localhost:8000"

    pile_id = make_pile(engine, tmp_path)
    committed_document(engine, tmp_path, pile_id)

    body = await call(server, "get_final_document", pile_id=pile_id)
    assert body["download_url"].startswith("http://localhost:8000/")
    assert "api:8000" not in body["download_url"]


async def test_the_stdio_entrypoint_speaks_the_protocol(engine: Engine, tmp_path: Path) -> None:
    """`main()` in a real subprocess, over real stdio framing.

    The Phase 6 tests drove the tools through an in-process HTTP transport,
    which is right for the tool behaviour and blind to everything around it —
    which is how a compose service that started an HTTP listener where the docs
    promised a stdio process went unnoticed. This starts the process the way
    `claude mcp add` does and completes a handshake.

    It also pins the rule that makes stdio work at all: **stdout is the
    protocol**. One log line written there and the client fails to parse a
    frame, so the assertion is that every stdout line is JSON-RPC.
    """
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "documents_analyst.mcp.server", "--transport", "stdio"],
        env={
            **os.environ,
            "MCP_API_BASE_URL": "http://127.0.0.1:1",  # nothing listens; no tool is called
            "DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "LOG_LEVEL": "INFO",
        },
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        info = await session.initialize()
        listed = await session.list_tools()

    assert info.server_info.name == "documents-analyst"
    assert len(listed.tools) == 7
    assert "approve_item" in {tool.name for tool in listed.tools}


async def test_an_unreachable_api_is_a_sentence_over_stdio_too(
    engine: Engine, tmp_path: Path
) -> None:
    """The failure a first-time user actually hits: the API is not running.

    A connection error must arrive as an instruction, not a traceback, because
    it lands in a model's context and the next action ("start the API") is
    knowable.
    """
    settings = settings_for(engine, tmp_path, mcp_api_base_url="http://127.0.0.1:1")
    server = build_server(settings, AnalystApi(settings))

    message = await refused(server, "list_piles")

    assert "cannot reach the analyst API" in message
    assert "make api" in message


def test_commit_applies_resolutions_before_it_renders() -> None:
    """The call site, not just the function — which is what was missing.

    `chosen_value_group` was written by the API and read by nobody: the logic to
    resolve a slot existed in one module and had no caller in another. A test
    that invokes `_apply_resolutions` directly would have passed against that
    codebase, so this reads `Commit.run` and asserts both that it calls the
    thing and that it calls it **before** loading the slots it renders. The
    order is the property: resolving after the read publishes the previous,
    still-disputed values.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "backend/src/documents_analyst/graph/nodes/commit.py"
    ).read_text()
    run_fn = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(run_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    ]
    assert "_apply_resolutions" in calls, "Commit never applies an approved choice"
    assert calls.index("_apply_resolutions") < calls.index("slot_repo.for_pile"), (
        "the slots are read before the approval is applied, so the render would "
        "publish the values the reviewer replaced"
    )


async def test_the_document_is_reachable_on_an_authenticated_instance(
    wire: Any, engine: Engine, tmp_path: Path
) -> None:
    """With `API_TOKEN` set, the URL is useless and the resource is the answer.

    A client holds `MCP_TOKEN`, not the API's credential, so following
    `download_url` gets a 401. The tool has to *advertise* the resource, because a
    client cannot read a URI nobody mentioned.
    """
    server, _, _ = await wire(api_token="locked")
    pile_id = make_pile(engine, tmp_path)
    path = committed_document(engine, tmp_path, pile_id)

    body = await call(server, "get_final_document", pile_id=pile_id)

    assert body["resource_uri"] == f"document://{pile_id}"
    assert body["download_url_needs_api_token"] is True

    payload = await server.read_resource(body["resource_uri"])
    content = payload[0].content if isinstance(payload, list) else payload
    assert bytes(content) == path.read_bytes()


async def test_a_client_without_the_api_token_cannot_use_the_url(
    wire: Any, engine: Engine, tmp_path: Path
) -> None:
    """Which is why the resource exists — stated as a test rather than assumed."""
    _, api, settings = await wire(api_token="locked")
    pile_id = make_pile(engine, tmp_path)
    committed_document(engine, tmp_path, pile_id)

    async with httpx.AsyncClient(
        transport=api._client._transport,  # noqa: SLF001 — the same app, no credential
        base_url=settings.mcp_api_base_url,
    ) as anonymous:
        refused_download = await anonymous.get(f"/piles/{pile_id}/deliverable/document")
    assert refused_download.status_code == 401


async def test_a_conflict_with_no_options_cannot_be_approved_at_all(
    wired: Any, engine: Engine, tmp_path: Path
) -> None:
    """An approval naming a group that does not exist cannot resolve anything.

    The check read `if available and chosen not in available`, so an item whose
    payload recorded no options accepted any number — and the approval then sat
    `approved` for ever while `Commit` logged that it chose a group with no live
    facts and the slot kept showing its disagreement. A decision that cannot be
    acted on should never have been accepted.
    """
    server, _, _ = wired
    pile_id = make_pile(engine, tmp_path)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test-worker", 90)
    assert epoch is not None
    with engine.begin() as conn:
        malformed = decision_repo.raise_decision(
            conn,
            Fence(pile_id=pile_id, epoch=epoch),
            kind="conflict",
            ref_id=str(uuid.uuid4()),
            evidence_quote=QUOTE,
            payload={"field_key": "recurring_price", "options": []},
        )
        lease.release(conn, pile_id, epoch)
    assert malformed is not None

    message = await refused(
        server, "approve_item", decision_id=malformed, confirm=QUOTE, chosen_value_group=999
    )

    assert "no options" in message
    with engine.begin() as conn:
        assert decision_repo.get(conn, malformed)["status"] == "pending"  # type: ignore[index]
