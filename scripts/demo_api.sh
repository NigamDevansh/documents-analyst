#!/usr/bin/env bash
#
# Drive the whole workflow over HTTP, with no UI.
#
# What it demonstrates, in order: the navigator, the register as we hold it,
# the claim → source map, the review queue, an approval that records who made
# it, the same approval refused a second time, the final document read back and
# verified, and the live event stream.
#
# It changes real state — an approval here is a real approval, and if a worker
# is running it will commit to the real final document. That is the point: this
# is the same door the dashboard and the MCP server use, not a read-only tour.
#
# Needs: `make api` (or the full stack) already running, and a pile that has
# been through a run. `make seed && make run` gets you one.

set -euo pipefail

API="${API:-http://localhost:8000}"
PILE_NAME="${1:-acme}"
ACTOR="${ACTOR:-$(git config user.name 2>/dev/null || echo demo)}"
# `set -u` treats an empty array expansion as unbound on bash 3.2, which is
# what macOS still ships. `${AUTH[@]+"${AUTH[@]}"}` is the portable spelling.
AUTH=()
[[ -n "${API_TOKEN:-}" ]] && AUTH=(-H "Authorization: Bearer ${API_TOKEN}")

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
get() { curl -fsS ${AUTH[@]+"${AUTH[@]}"} "$API$1"; }

command -v jq >/dev/null || { echo "this script needs jq"; exit 1; }

say "0. Is it up, and is it configured?"
get /health | jq '{phase, degraded, database: .database.reachable, events: .events.listening,
                   token_required: .auth.token_required, missing: .missing_configuration}'

say "1. The navigator — every pile with its counts"
get /piles | jq '.piles[] | {name, status, documents, pending_decisions, open_conflicts, open_findings}'

PILE=$(get /piles | jq -r --arg n "$PILE_NAME" '.piles[] | select(.name == $n) | .id')
if [[ -z "$PILE" || "$PILE" == "null" ]]; then
  echo "no pile named '$PILE_NAME'. Run: make seed && make run"
  exit 1
fi
echo "pile $PILE_NAME = $PILE"

say "2. The register as we hold it"
get "/piles/$PILE/deliverable" | jq '{version, document, slots: [.slots[] | {label, state,
    value: (.current.text // "⚠ disputed — no value chosen"),
    sources: (.current.citation_count // 0)}]}'

say "3. Every claim, and the span in the source that proves it"
get "/piles/$PILE/citations" | jq '{count, first_three: [.citations[:3][] |
    {field: (.field_key // .subject_free), quote: .source_quote,
     at: "\(.filename) [\(.char_start):\(.char_end)]"}]}'

say "4. Prove one of them against the source text itself"
get "/piles/$PILE/citations" | jq -r '.citations[0] | "\(.source_document_id) \(.char_start) \(.char_end)"' \
  | while read -r doc start end; do
      [[ "$doc" == "null" ]] && { echo "no citations yet — has the pile been through a run?"; break; }
      get "/documents/$doc/text" | jq -r --argjson s "$start" --argjson e "$end" \
        '"extracted text[\($s):\($e)] = \(.text[$s:$e])"'
    done

say "5. The review queue"
get "/piles/$PILE/decisions" | jq '{pending, items: [.items[] | {id, kind, relationship,
    detected_by, recommendation}]}'

ITEM=$(get "/piles/$PILE/decisions" | jq -r '.items[0].id // empty')
if [[ -z "$ITEM" ]]; then
  say "Nothing is waiting for review, so the approval steps are skipped."
else
  say "6. An approval with no name is refused — decided_by can never be blank"
  curl -sS -o /dev/null -w '  POST /decisions/{id}/approve with no X-Actor → HTTP %{http_code}\n' \
    ${AUTH[@]+"${AUTH[@]}"} -X POST "$API/decisions/$ITEM/approve" -H 'content-type: application/json' -d '{}'

  say "7. Approving as '$ACTOR'"
  curl -fsS ${AUTH[@]+"${AUTH[@]}"} -X POST "$API/decisions/$ITEM/approve" \
    -H "X-Actor: $ACTOR" -H 'content-type: application/json' \
    -d '{"note": "approved from scripts/demo_api.sh"}' | jq '.decision | {status, decided_by, decided_at}'

  say "8. The same item again — the second reviewer is told, not silently ignored"
  curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$API/decisions/$ITEM/reject" \
    -H 'X-Actor: somebody-else' -H 'content-type: application/json' -d '{}' \
    -w '\n  → HTTP %{http_code}\n' | jq -R 'fromjson? // .'
fi

say "9. The final document, read back off disk and compared to the record"
curl -sS ${AUTH[@]+"${AUTH[@]}"} "$API/piles/$PILE/deliverable/verify" \
  | jq '{version, verified, sections_checked, problems, document_sha256}' \
  || echo "  nothing committed yet"

say "10. What this pile has cost"
get "/piles/$PILE/cost" | jq '{cost_usd, calls, failures,
    by_node: [.by_node[] | {node, calls, cost_usd}]}'

say "11. The live stream — 8 seconds, replaying this pile's history first"
echo "  (Last-Event-ID is stage_events.id, so a reconnect resumes across runs)"
curl -fsS -N --max-time 8 ${AUTH[@]+"${AUTH[@]}"} "$API/piles/$PILE/events" | head -40 || true

say "Done. The same endpoints back the dashboard and the MCP server."
