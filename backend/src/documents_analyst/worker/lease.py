"""One pile, one worker — enforced by an epoch, not by a lock.

A TTL lease alone is not enough: a frozen worker can wake after its lease
expired and keep writing. So every lease grant increments an epoch, and every
write carries the epoch it believes it holds:

    UPDATE ... WHERE lease_epoch = :mine   →   rowcount 0 means we lost it

The stale worker's writes are rejected by the ROW, not by its own good
behaviour. Same pattern as ZooKeeper's zxid, Kafka's generation.id and a Raft
term — fencing beats politeness because a frozen process cannot be polite.

See docs/WORKER_RUNTIME.md § "The lease, and why it is released at the human gate".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, CursorResult
from sqlalchemy.sql.elements import TextClause


class LostLease(Exception):
    """Raised when a fenced write affects zero rows. Always fatal to the run."""


ACQUIRE = text("""
    UPDATE piles
       SET lease_owner_id   = :worker_id,
           lease_epoch      = lease_epoch + 1,
           lease_expires_at = now() + make_interval(secs => :ttl_s),
           updated_at       = now()
     WHERE id = :pile_id
       AND (lease_expires_at IS NULL OR lease_expires_at < now())
 RETURNING lease_epoch
""")
"""Taking a lease is one statement, and the WHERE clause is the whole safety
argument. `lease_expires_at < now()` is Postgres against its own clock, so two
skewed-clock workers can't both believe the lease is free — and the UPDATE takes a
row lock, so there is no check-then-act window. The epoch increments on every
grant, including a takeover, which is what makes the old holder's writes
identifiable as stale rather than merely late."""

HEARTBEAT = text("""
    UPDATE piles
       SET lease_expires_at = now() + make_interval(secs => :ttl_s),
           updated_at       = now()
     WHERE id = :pile_id
       AND lease_epoch = :epoch
       AND lease_expires_at > now()
 RETURNING lease_epoch
""")

RELEASE = text("""
    UPDATE piles
       SET lease_owner_id   = NULL,
           lease_expires_at = NULL,
           updated_at       = now()
     WHERE id = :pile_id
       AND lease_epoch = :epoch
       AND lease_expires_at > now()
""")


def acquire(conn: Connection, pile_id: str, worker_id: str, ttl_s: int) -> int | None:
    """Returns the new epoch, or None when someone else holds it. None is ordinary,
    not an error: with several workers sharing a queue, most attempts on a busy pile
    fail and the worker goes looking for another one."""
    row = conn.execute(
        ACQUIRE, {"pile_id": pile_id, "worker_id": worker_id, "ttl_s": ttl_s}
    ).first()
    return int(row.lease_epoch) if row else None


def heartbeat(conn: Connection, pile_id: str, epoch: int, ttl_s: int) -> bool:
    """False means the lease expired or was taken; the caller stops immediately.
    Fenced on the epoch like every write: a worker frozen past its TTL can't extend
    an expired lease and must acquire again for a new epoch, so a paused worker
    can't resurrect its old authority just by waking first."""
    row = conn.execute(HEARTBEAT, {"pile_id": pile_id, "epoch": epoch, "ttl_s": ttl_s}).first()
    return row is not None


def release(conn: Connection, pile_id: str, epoch: int) -> None:
    """Fenced too, and silent when it does nothing. A worker whose lease expired or
    was taken must not clear the NEW holder's ownership on its way out — that would
    hand the pile to a third worker mid-run. Zero rows is the expected outcome of a
    late release, not an error."""
    conn.execute(RELEASE, {"pile_id": pile_id, "epoch": epoch})


@dataclass(frozen=True, slots=True)
class Fence:
    """Who is allowed to write to this pile, right now. Passed down through
    ingestion instead of a bare `pile_id`, so a write can't be issued without also
    stating which epoch it believes it holds — a type-level "don't forget to
    check": no signature accepts the pile and omits the authority."""

    pile_id: str
    epoch: int


HOLDS_LEASE = (
    "EXISTS (SELECT 1 FROM piles "
    "WHERE id = :pile_id AND lease_epoch = :epoch AND lease_expires_at > now())"
)
"""The fencing predicate, appended to every mutating ingestion statement. Evaluated
in the same statement as the write, so there is no check-then-act window; the
expiry check matters equally, so a worker that wakes past its TTL can't write in
the gap before another worker acquires the next epoch."""


def fenced(conn: Connection, statement: TextClause, params: dict[str, Any]) -> CursorResult[Any]:
    """Run a write guarded by `HOLDS_LEASE`, and raise if it hit nothing. The one
    helper every mutating write goes through — checking rowcount by hand works until
    someone forgets, and a forgotten check is a stale worker silently overwriting a
    live run, which looks like corrupt data rather than a concurrency bug."""
    result = conn.execute(statement, params)
    if result.rowcount == 0:
        raise LostLease(
            f"fenced write affected 0 rows — epoch {params.get('epoch')} is stale for pile "
            f"{params.get('pile_id')}. Another worker holds it; stop rather than write over it."
        )
    return result


def fenced_update(conn: Connection, statement: TextClause, params: dict[str, Any]) -> None:
    """Backwards-compatible alias for a statement that carries its own
    `lease_epoch = :epoch` predicate."""
    fenced(conn, statement, params)
