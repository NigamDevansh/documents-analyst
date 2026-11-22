import { useEffect, useRef, useState } from "react";
import type { StageEvent } from "../api/types";

/** The live pipeline over SSE. `EventSource` reconnects and replays its
 * `Last-Event-ID` on its own — exactly the server's contract — so nothing here
 * tracks a cursor. It cannot send an `Authorization` header, so an instance with
 * `API_TOKEN` set needs the fetch-stream variant (noted in the docs).
 */
export function useEvents(pileId: string | null): {
  events: StageEvent[];
  live: boolean;
} {
  const [events, setEvents] = useState<StageEvent[]>([]);
  const [live, setLive] = useState(false);
  const seen = useRef(new Set<number>());

  useEffect(() => {
    setEvents([]);
    seen.current = new Set();
    if (!pileId) return;

    const source = new EventSource(`/piles/${pileId}/events`);
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);

    const onFrame = (raw: MessageEvent) => {
      // The server names every frame after its stage type, and SSE dispatches a
      // named frame ONLY to a listener for that name. `onmessage` never sees
      // them — so a type missing from the list below is not "handled by the
      // fallback", it is dropped — the timeline would stop at the last stage a
      // reviewer could see and never show the commit finishing.
      if (!raw.lastEventId) return;
      const id = Number(raw.lastEventId);
      if (seen.current.has(id)) return; // a replay after reconnect
      seen.current.add(id);
      try {
        const body = JSON.parse(raw.data) as Omit<StageEvent, "id">;
        setEvents((current) => [...current, { ...body, id }]);
      } catch {
        /* a frame we cannot parse is a frame we skip, not a broken stream */
      }
    };

    // `addEventListener` per type, because SSE dispatches by `event:` name and
    // the server names every frame after its stage.
    const types = new Set<string>();
    const attach = (type: string) => {
      if (types.has(type)) return;
      types.add(type);
      source.addEventListener(type, onFrame as EventListener);
    };
    for (const type of KNOWN_TYPES) attach(type);
    // Frames with no `event:` field, which the server does not currently send.
    // Kept because it costs nothing and an unnamed frame is still an event.
    source.onmessage = onFrame;

    return () => {
      source.close();
      setLive(false);
    };
  }, [pileId]);

  return { events, live };
}

/** Every frame the backend emits. **This list is the subscription, not a hint.**
 *
 * A named SSE frame reaches only a listener registered for that exact name, so
 * anything missing here is silently discarded — there is no wildcard and no
 * fallback. `test_api.py::test_the_dashboard_subscribes_to_every_event_the_backend_emits`
 * fails the build when the backend gains a type this list does not have, because
 * the failure mode is otherwise invisible: a stream that looks healthy and is
 * quietly missing the end of the run.
 */
const KNOWN_TYPES = [
  "run.started",
  "run.blocked",
  "run.retry_requested",
  "run.incomplete",
  "run.sources_changed",
  "run.coverage_updated",
  "run.resumed",
  "run.paused",
  "run.finished",
  "run.lost_lease",
  "stage.ingest.done",
  "stage.extract.document",
  "stage.extract.done",
  "stage.match.done",
  "stage.classify.done",
  "stage.audit.done",
  "stage.gate.waiting",
  "stage.gate.clear",
  "stage.gate.resumed",
  "stage.resolve.done",
  "stage.commit.resolved",
  "stage.commit.applied",
  "stage.commit.done",
  "stage.reconcile.done",
  "stage.reconcile.unverified",
  "ping",
];
