"""Which documents get compared with which — the grouping rule.

`010` made the party set the whole of an agreement's identity, which is what
makes a conflict a conflict. It also decides too much: two companies sign more
than one contract, and an MSA and an NDA between the same pair became ONE
agreement whose different terms were reported as conflicts.

The rule these tests hold in place:

  * a standalone contract type gets its own agreement
  * a dependent document — amendment, side letter, statement of work — joins
    the agreement it modifies, because an amendment that is not compared with
    what it amends is not an amendment
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from documents_analyst.db import migrate
from documents_analyst.db.repo import agreements as agreement_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.worker import lease
from documents_analyst.worker.lease import Fence
from documents_analyst_core.entities.family import detect_family

pytestmark = pytest.mark.db

SCRATCH = "analyst_family_test_" + uuid.uuid4().hex[:12]


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
    migrate.run(f"{base}/{SCRATCH}")
    made = create_engine(f"{base}/{SCRATCH}", future=True)
    yield made
    made.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
    admin.dispose()


@pytest.fixture
def pile(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Fence]:
    directory = tmp_path_factory.mktemp("family")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE piles CASCADE"))
        pile_id = pile_repo.ensure(conn, name=directory.name, watch_path=directory)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test", ttl_s=90)
    assert epoch is not None
    yield Fence(pile_id=pile_id, epoch=epoch)


PARTIES = ("acme", "beta logistics")


def _ensure(engine: Engine, fence: Fence, *, title: str, family: str | None) -> str:
    with engine.begin() as conn:
        return agreement_repo.ensure(conn, fence, parties=PARTIES, title=title, family=family)


# ── detection, offline ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ("MASTER SERVICES AGREEMENT\nbetween Acme and Beta", "services"),
        ("MUTUAL NON-DISCLOSURE AGREEMENT\nbetween Acme and Beta", "nda"),
        ("LEASE AGREEMENT\nLandlord and Tenant", "lease"),
        ("EMPLOYMENT AGREEMENT\nbetween the Company and Executive", "employment"),
        ("SUPPLY AGREEMENT\nbetween X and Y", "supply"),
        # A dependent that names its parent takes the parent's family, so it
        # lands on the same agreement the parent did.
        ("AMENDMENT NO. 2 TO SUPPLY AGREEMENT\nis made between X and Y", "supply"),
        ("THIS FIRST AMENDMENT to the Master Services Agreement dated 1 Jan", "services"),
        # A dependent that names no parent declares nothing, and says so.
        ("STATEMENT OF WORK NO. 3\nServices to be performed", None),
        ("SIDE LETTER\nTo: Acme Corporation", None),
        ("INVOICE 10432\nAmount due on receipt", None),
        ("", None),
    ],
)
def test_family_is_read_from_the_title(head: str, expected: str | None) -> None:
    assert detect_family(head) == expected


def test_a_contract_that_mentions_an_exhibit_is_not_a_dependent() -> None:
    """Dependency is decided on the heading, not on the whole preamble.

    Every contract refers to exhibits, schedules and amendments in its body.
    Testing for those words across the opening window classified a Master
    Services Agreement as a dependent — which put the MSA in one agreement and
    its own SOW, invoice and amendment in another, so none of them were ever
    compared with it.
    """
    msa = (
        "1. Parties and Agreement\n\n"
        "This Master Services Agreement is entered into between Acme Corporation "
        "and Beta Logistics LLC. Pricing is set out in Exhibit A and the service "
        "schedule is attached. Any amendment must be in writing.\n"
    )
    assert detect_family(msa) == "services"


def test_the_specific_phrase_wins_over_the_general_one() -> None:
    """`master services agreement` must not be read as a bare `master agreement`,
    and `lease agreement` must be tried before the `lease` that also matches
    "leasehold improvements"."""
    assert detect_family("MASTER SERVICES AGREEMENT") == "services"
    assert detect_family("LEASE AGREEMENT for leasehold improvements") == "lease"


# ── grouping, against the database ────────────────────────────────────────


def test_two_kinds_of_contract_between_the_same_parties_are_two_agreements(
    engine: Engine, pile: Fence
) -> None:
    """The bug this exists for.

    An MSA and an NDA between the same pair were one agreement, so their
    different confidentiality periods and effective dates reached a reviewer as
    conflicts — a contradiction manufactured by the grouping rather than found
    in the contracts.
    """
    msa = _ensure(engine, pile, title="01_msa.md", family="services")
    nda = _ensure(engine, pile, title="09_nda.md", family="nda")
    assert msa != nda


def test_an_amendment_joins_the_contract_it_amends(engine: Engine, pile: Fence) -> None:
    """Named parent: the family comes from the title and matches directly."""
    supply = _ensure(engine, pile, title="supply.md", family="supply")
    amendment = _ensure(
        engine,
        pile,
        title="amendment.md",
        family=detect_family("AMENDMENT NO. 2 TO SUPPLY AGREEMENT"),
    )
    assert amendment == supply


def test_a_dependent_with_no_named_parent_joins_the_one_agreement_there_is(
    engine: Engine, pile: Fence
) -> None:
    """A bare "Statement of Work No. 3" says nothing about its parent.

    Left to start its own agreement, a SOW's fee would never be compared with
    the master agreement's — the pile would look clean while holding a real
    disagreement.
    """
    msa = _ensure(engine, pile, title="01_msa.md", family="services")
    sow = _ensure(engine, pile, title="02_sow.md", family=detect_family("STATEMENT OF WORK NO. 3"))
    assert sow == msa


def test_a_dependent_does_not_guess_between_two_agreements(engine: Engine, pile: Fence) -> None:
    """Two candidates and nothing in the document says which.

    Filing it under either would be a guess that silently decides what gets
    compared, so it gets its own agreement instead — visible, and splittable by
    a human later.
    """
    msa = _ensure(engine, pile, title="01_msa.md", family="services")
    nda = _ensure(engine, pile, title="09_nda.md", family="nda")
    orphan = _ensure(engine, pile, title="sow.md", family=None)
    assert orphan not in (msa, nda)


def test_the_same_contract_twice_is_still_one_agreement(engine: Engine, pile: Fence) -> None:
    """The property `010` bought, unchanged: two documents of one contract are
    compared. Losing this would mean no conflict is ever found."""
    first = _ensure(engine, pile, title="01_msa.md", family="services")
    second = _ensure(engine, pile, title="01_msa_signed.pdf", family="services")
    assert first == second


def test_the_first_title_survives(engine: Engine, pile: Fence) -> None:
    """A later document in the group is usually an amendment; letting its name
    overwrite the master agreement's would rename the group after its least
    important member."""
    _ensure(engine, pile, title="01_msa.md", family="services")
    _ensure(engine, pile, title="amendment.md", family="services")
    with engine.connect() as conn:
        titles = [r.title for r in conn.execute(text("SELECT title FROM agreements"))]
    assert titles == ["01_msa.md"]
