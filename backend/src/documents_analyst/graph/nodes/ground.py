"""Gate 1 — and the reason this file has no node in it.

Grounding is a function, not a node: it sits directly around the model output in
`provenance.extract`, the only place a candidate becomes a fact. A separate node
would fail the state rule — nodes talk through the checkpointed state, which
forbids business data, so candidate facts would have to travel through Postgres
as unverified claims one step before being verified. Keeping the gate inside the
extraction call also means no window exists where an ungrounded candidate is
reachable: `extract_facts` returns `GroundedFact`s and `Rejection`s, and
`repo/facts.py` accepts only the former. The check cannot be skipped, because it
is not a route.

See `graph/nodes/extract.py` and `portable/verify.py`.
"""

from __future__ import annotations
