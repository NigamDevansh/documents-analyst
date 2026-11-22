import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";
import type { StageEvent } from "../api/types";

/** One line: what the pile is doing right now — whether it is working and what
 * it is on, not a scrubber through forty stage records. The history is one click
 * away for when the question is "what happened". The loader is driven by the
 * pile's own `status` (the worker holds the lease or not), not by whether this
 * browser's connection is open.
 *
 * See docs/UI_AND_LINKING.html.
 */
export function Activity({
  events,
  live,
  running,
}: {
  events: StageEvent[];
  /** The SSE connection. Says whether this page is hearing about the pile — not
   * whether anything is being done to it. */
  live: boolean;
  /** The worker has the lease right now. */
  running: boolean;
}) {
  const [open, setOpen] = useState(false);
  const latest = events[events.length - 1];
  const working = running && (latest === undefined || !ENDED.has(latest.type));

  return (
    <div className="activity">
      <button
        className="activity__line"
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        disabled={events.length === 0}
      >
        {working ? (
          <i className="spinner" aria-hidden />
        ) : (
          // Not working. The dot says whether we'd even know it started — a
          // dropped stream is the one quiet-for-no-reason case.
          <span className={`dot dot--${live ? "idle" : "error"}`} title={live ? "idle" : "not connected"} />
        )}
        <span className="activity__what">{latest ? phrase(latest) : "Nothing has run for this pile yet"}</span>
        {events.length > 0 && (
          <>
            <span className="spacer" />
            <span className="stat">{new Date(latest!.created_at).toLocaleTimeString()}</span>
            <span className="activity__chevron" aria-hidden>
              {open ? "▾" : "▸"}
            </span>
          </>
        )}
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.ol
            className="activity__history"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          >
            {/* Newest first. Opening this is asking "what just happened", and
              * the answer is at the end of the list, not the start. */}
            {[...events].reverse().map((event) => (
              <li key={event.id}>
                <span className="activity__when">
                  {new Date(event.created_at).toLocaleTimeString()}
                </span>
                <span className="activity__what">{phrase(event)}</span>
                <span className="activity__detail">{summarise(event.payload)}</span>
              </li>
            ))}
          </motion.ol>
        )}
      </AnimatePresence>
    </div>
  );
}

const ENDED = new Set(["run.finished", "run.paused", "run.lost_lease"]);

/** A stage event as something a person would say.
 *
 * `stage.extract.done` is our word for it; "read the documents" is what
 * happened. Anything unrecognised falls back to the raw type rather than being
 * hidden — a new stage should look unfamiliar, not invisible.
 */
function phrase(event: StageEvent): string {
  const payload = event.payload ?? {};
  // A run's closing event carries what it spent. There is no spending limit, so this
  // is the answer to "what did that cost", not a warning.
  const cost = RUN_END.has(event.type) ? spent(payload) : "";
  const said = SAID[event.type];
  if (said) return `${said}${cost}`;
  if (event.type === "run.incomplete") return `Analysis incomplete: ${payload.remaining} document(s) remain. The previous final document is retained.${cost}`;
  if (event.type === "run.retry_requested") return "Retry requested";
  if (event.type === "run.blocked") return String(payload.reason || "Analysis paused: check configuration and retry");
  if (event.type === "stage.extract.document") {
    return `Reading ${payload.filename ?? "a document"}`;
  }
  return event.type;
}

const RUN_END = new Set(["run.finished", "run.paused", "run.incomplete"]);

function spent(payload: Record<string, unknown>): string {
  if (payload.cost_usd === undefined) return "";
  const dollars = `$${Number(payload.cost_usd).toFixed(4)}`;
  const unpriced = Number(payload.unpriced_calls ?? 0);
  return unpriced
    ? ` — this run cost ${dollars} plus ${unpriced} unpriced call${unpriced === 1 ? "" : "s"}`
    : ` — this run cost ${dollars}`;
}

const SAID: Record<string, string> = {
  "run.started": "Started",
  "run.resumed": "Resumed after review",
  "run.paused": "Waiting for your review",
  "run.finished": "Finished",
  "run.lost_lease": "Handed over to another worker",
  "run.sources_changed": "Files in the folder changed",
  "run.coverage_updated": "Updated the list of sources in the final document",
  "stage.ingest.done": "Read the folder",
  "stage.extract.done": "Read the documents",
  "stage.match.done": "Matched terms across documents",
  "stage.classify.done": "Looked for disagreements",
  "stage.audit.done": "Checked the rulebook",
  "stage.gate.waiting": "Waiting for your review",
  "stage.gate.clear": "Review complete",
  "stage.gate.resumed": "Resumed after review",
  "stage.resolve.done": "Restored the final document from the record",
  "stage.commit.resolved": "Applied your decisions",
  "stage.commit.applied": "Updated the final document",
  "stage.commit.done": "Final document up to date",
  "stage.reconcile.done": "Read the final document back and verified it",
  "stage.reconcile.unverified": "Could not verify the final document",
};

/** A payload as one readable line. `{"documents": 8, "epoch": 4}` → `documents 8 · epoch 4`. */
function summarise(payload: Record<string, unknown>): string {
  return Object.entries(payload ?? {})
    .filter(([, value]) => value !== null && value !== "" && typeof value !== "object")
    .map(([key, value]) => `${key} ${value}`)
    .join("  ·  ");
}
