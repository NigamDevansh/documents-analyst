import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";
import type { DecisionItem } from "../api/types";

/** The human gate, as a stack of cards. What the API refuses is refused here
 * first — not to duplicate the rule, but so the answer is instant: a conflict
 * needs a chosen value (422 without one), an amendment does not. The card carries
 * its evidence — a queue you must click through to read is one people approve
 * without reading.
 *
 * See docs/UI_AND_LINKING.html.
 */
export function ReviewQueue({
  items,
  onDecide,
  busy,
}: {
  items: DecisionItem[];
  onDecide: (item: DecisionItem, approve: boolean, group?: number) => Promise<void>;
  busy: string | null;
}) {
  if (items.length === 0) {
    return (
      <div className="empty">
        <h3>Nothing waiting</h3>
        <p>
          Every question this pile raised has been answered. New documents may raise more — the
          worker resumes the run once the queue is empty.
        </p>
      </div>
    );
  }

  return (
    <div className="stack">
      <AnimatePresence mode="popLayout">
        {items.map((item) => (
          <Card key={item.id} item={item} onDecide={onDecide} busy={busy === item.id} />
        ))}
      </AnimatePresence>
    </div>
  );
}

function Card({
  item,
  onDecide,
  busy,
}: {
  item: DecisionItem;
  onDecide: (item: DecisionItem, approve: boolean, group?: number) => Promise<void>;
  busy: boolean;
}) {
  const options = item.payload?.options ?? [];
  const recommended = item.payload?.recommended_value_group ?? null;
  const [picked, setPicked] = useState<number | null>(recommended);

  // A conflict cannot be approved without naming a winner — the API says so
  // with a 422, and the button says so by being disabled with a reason.
  const needsChoice = item.kind === "conflict" && options.length > 0;
  const canApprove = !needsChoice || picked !== null;

  // "Approve" is the right word for a value someone is agreeing to and the
  // wrong one for a deletion — approving here removes a row from the register.
  // The API call is identical; only the label changes, because a button that
  // hides what it does is how a reviewer deletes something by accident.
  const removal = item.kind === "orphaned_claims";
  const rejectLabel = removal ? "Keep it" : "Reject";

  // A removal asks twice. It is the one decision here that destroys something —
  // the row leaves the register and the slot is retired — and there is no undo
  // afterwards, because undoing it would mean re-seeding the document. Every
  // other decision on this screen changes a value that can be changed back.
  //
  // Two clicks on one button rather than a modal: a dialog would be heavier
  // than the risk and would train people to dismiss dialogs.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    // Arming times out. A button left saying "Confirm" is a trap for the next
    // person to look at the screen.
    const id = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(id);
  }, [armed]);
  const approveLabel = removal
    ? armed
      ? "Confirm — remove it"
      : "Remove the row"
    : "Approve";

  return (
    <motion.article
      className="card"
      layout
      initial={{ opacity: 0, y: 10, scale: 0.99 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.97, transition: { duration: 0.18 } }}
      transition={{ duration: 0.32, ease: [0.16, 1, 0.3, 1] }}
    >
      <header className="card__head">
        <span className={`pill pill--${item.kind === "amendment" ? "resolved" : "disputed"}`}>
          {item.kind.replace(/_/g, " ")}
        </span>
        {item.payload?.field_key && <span className="card__kind">{item.payload.field_key}</span>}
        <span className="spacer" />
        {item.detected_by && <span className="stat">detected by {item.detected_by}</span>}
      </header>

      <div className="card__body">
        {item.recommendation && <p style={{ margin: 0 }}>{item.recommendation}</p>}
        {item.evidence_quote && <blockquote className="quote">“{item.evidence_quote}”</blockquote>}

        {options.length > 0 && (
          <div className="options" role="radiogroup" aria-label="Which value wins">
            {options.map((option) => (
              <motion.button
                key={option.value_group}
                className="choice"
                data-picked={picked === option.value_group}
                onClick={() => setPicked(option.value_group)}
                whileTap={{ scale: 0.995 }}
                role="radio"
                aria-checked={picked === option.value_group}
                tabIndex={picked === option.value_group || (picked === null && option === options[0]) ? 0 : -1}
                onKeyDown={(event) => {
                  const index = options.indexOf(option);
                  const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
                    : event.key === "ArrowRight" || event.key === "ArrowDown" ? (index + 1) % options.length
                    : event.key === "ArrowLeft" || event.key === "ArrowUp" ? (index + options.length - 1) % options.length : -1;
                  if (next < 0) return;
                  const chosen = options[next]; if (!chosen) return;
                  event.preventDefault(); setPicked(chosen.value_group);
                  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="radio"]')[next]?.focus();
                }}
              >
                <span className="choice__radio">
                  <AnimatePresence>
                    {picked === option.value_group && (
                      <motion.i
                        initial={{ scale: 0 }}
                        animate={{ scale: 1 }}
                        exit={{ scale: 0 }}
                        transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
                      />
                    )}
                  </AnimatePresence>
                </span>
                <span>
                  <span className="option__value">{option.value_text}</span>
                  <span className="stat" style={{ display: "block", marginTop: 2 }}>
                    {saidBy(option.sources) || "no source recorded"}
                    {recommended === option.value_group && " · recommended"}
                  </span>
                </span>
              </motion.button>
            ))}
          </div>
        )}
      </div>

      <footer className="card__foot">
        {needsChoice && picked === null && (
          <span className="stat">Choose which value wins to approve</span>
        )}
        {armed && (
          <span className="stat" role="status">
            This deletes the row from the register. Click again to confirm.
          </span>
        )}
        <span className="spacer" />
        <button
          className="btn btn--ghost"
          onClick={() => onDecide(item, false)}
          disabled={busy}
        >
          {rejectLabel}
        </button>
        <button
          className={`btn btn--primary${armed ? " btn--danger" : ""}`}
          onClick={() => {
            if (removal && !armed) {
              setArmed(true);
              return;
            }
            void onDecide(item, true, picked ?? undefined);
          }}
          disabled={busy || !canApprove}
        >
          {busy ? "Saving…" : approveLabel}
        </button>
      </footer>
    </motion.article>
  );
}

/** "02_sow.md, 04_amendment.md" — and "02_sow.md (×2)" when one document says
 * it twice.
 *
 * One entry per citation arrives here, so a contract stating the same figure in
 * two clauses rendered as `02_sow.md, 02_sow.md`. That is real evidence, not a
 * duplicate row, but it reads as a bug and makes a reviewer distrust the rest of
 * the card. Counting says the same thing without looking broken.
 */
function saidBy(sources: { filename: string }[]): string {
  const counts = new Map<string, number>();
  for (const source of sources) {
    counts.set(source.filename, (counts.get(source.filename) ?? 0) + 1);
  }
  return [...counts]
    .map(([filename, n]) => (n > 1 ? `${filename} (\u00d7${n})` : filename))
    .join(", ");
}
