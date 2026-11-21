/** What a batch of stage events means for the data on screen. Applied to the
 * whole batch, not just the newest event: a stream replays a pile's history in
 * one chunk, and keying off the last frame ignores every other one.
 */

import type { StageEvent } from "./types";

/** Events after `handledId`, in order. Ids are `stage_events.id`, monotonic. */
export function unhandled(events: StageEvent[], handledId: number): StageEvent[] {
  return events.filter((event) => event.id > handledId);
}

/** The highest id in a batch, or the one already handled if it is empty. */
export function highestId(events: StageEvent[], fallback = 0): number {
  return events.reduce((top, event) => (event.id > top ? event.id : top), fallback);
}

/** A stage that changed something a reader can see.
 *
 * `stage.extract.document` deliberately does not match: it fires per file and
 * says the worker is still reading, not that there is anything new to show. The
 * pile *summary* is refreshed on any event at all — that is one small query and
 * it carries `status`, which is what the header pill and the sidebar dot are —
 * but the five per-pile reads wait for this.
 */
export function changedSomething(events: StageEvent[]): boolean {
  return events.some((event) => /(done|clear|resolved|seeded|finished|waiting)/.test(event.type));
}
