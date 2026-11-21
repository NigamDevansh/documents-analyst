import { describe, expect, it } from "vitest";
import { changedSomething, highestId, unhandled } from "./refresh";
import type { StageEvent } from "./types";

const event = (id: number, type: string): StageEvent => ({
  id,
  run_id: "r",
  seq: id,
  type,
  payload: {},
  created_at: "2026-08-16T00:00:00Z",
});

describe("which events force a refetch", () => {
  it("sees a stage that changed something even when it is not the newest event", () => {
    // The bug, exactly. An SSE stream that has never connected replays the
    // whole history in one chunk; React renders that burst once. Judging it by
    // its last frame — a `stage.extract.document`, because extraction is the
    // long part of a run and fires per file — threw away the `ingest.done` two
    // frames earlier, and the pile stayed on screen as `idle` while it ran.
    const burst = [
      event(1, "run.started"),
      event(2, "stage.ingest.done"),
      event(3, "stage.extract.document"),
      event(4, "stage.extract.document"),
    ];
    expect(changedSomething(burst)).toBe(true);
    expect(changedSomething(burst.slice(-1))).toBe(false);
  });

  it("does not refetch for a run that is only still reading", () => {
    expect(changedSomething([event(9, "stage.extract.document")])).toBe(false);
  });

  it("counts a paused run as something to look at", () => {
    expect(changedSomething([event(9, "stage.gate.waiting")])).toBe(true);
  });
});

describe("the cursor over events already acted on", () => {
  it("returns only what arrived since", () => {
    const events = [event(1, "run.started"), event(2, "stage.ingest.done"), event(3, "run.paused")];
    expect(unhandled(events, 1).map((e) => e.id)).toEqual([2, 3]);
    expect(unhandled(events, 3)).toEqual([]);
  });

  it("holds its place when a batch is empty, rather than rewinding to zero", () => {
    expect(highestId([], 7)).toBe(7);
    expect(highestId([event(4, "run.started")], 7)).toBe(7);
    expect(highestId([event(9, "run.started")], 7)).toBe(9);
  });
});
