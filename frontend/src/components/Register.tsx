import { AnimatePresence, motion } from "framer-motion";
import type { Citation, Register as RegisterData, Slot } from "../api/types";

/** The register, set as a document rather than a table — a value in a cell reads
 * like a database row, the same value under its heading reads like a contract.
 * A disputed term shows every value and no answer, not the first with a warning:
 * a skimming reader would otherwise carry away a number nobody chose.
 *
 * See README.md § "The dashboard".
 */
export function Register({
  data,
  citations,
  onCite,
  activeSlot,
  waitingForKeys = false,
}: {
  data: RegisterData;
  citations: Citation[];
  onCite: (citation: Citation) => void;
  activeSlot: string | null;
  /** No extraction key configured. The empty state must not promise a pass
   * that cannot happen — a stranger's first run sits here for ever otherwise,
   * being told the worker will get to it. */
  waitingForKeys?: boolean;
}) {
  const bySlot = new Map<string, Citation[]>();
  for (const citation of citations) {
    if (!citation.slot_id) continue;
    const list = bySlot.get(citation.slot_id) ?? [];
    list.push(citation);
    bySlot.set(citation.slot_id, list);
  }

  return (
    <div className="sheet">
      <motion.h2
        className="sheet__title"
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      >
        Contract Register
      </motion.h2>
      <p className="sheet__caveat">
        Generated and kept up to date from the source contracts. Every value carries a citation
        checked against the document it came from. It is not a legal opinion and certifies
        nothing.
      </p>

      {data.slots.length === 0 ? (
        <div className="empty">
          <h3>No terms yet</h3>
          {waitingForKeys ? (
            <p>
              The documents have been read and hashed, and extraction is waiting on a model key.
              Add one to <code>.env</code>, restart, and this pile is picked up where it stopped —
              nothing is re-read that has already been read.
            </p>
          ) : (
            <p>
              This pile has been discovered but not extracted. The worker picks it up on its next
              pass — the status line below shows when.
            </p>
          )}
        </div>
      ) : (
        <div>
          {/* Grouped by contract. The API returns slots already ordered by
            * agreement, so this only has to draw the boundary — and it has to:
            * a pile is a folder of contracts, and three agreements' payment
            * terms in one flat list read as one term contradicting itself. */}
          {byAgreement(data.slots).map(([agreement, slots, name], group) => (
            <section key={agreement} className="agreement">
              <h3 className="agreement__name">{name}</h3>
              {slots.map((slot, index) => (
                <Term
                  key={slot.slot_id}
                  slot={slot}
                  index={group * 2 + index}
                  citations={bySlot.get(slot.slot_id) ?? []}
                  onCite={onCite}
                  active={activeSlot === slot.slot_id}
                />
              ))}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

function Term({
  slot,
  index,
  citations,
  onCite,
  active,
}: {
  slot: Slot;
  index: number;
  citations: Citation[];
  onCite: (citation: Citation) => void;
  active: boolean;
}) {
  const disputed = slot.state === "disputed" || slot.state === "contested";
  // "Sources disagree" was said about every disputed term, including the ones
  // where there is one source. A filing describing six employment agreements
  // states two different terms in one sentence — both true, both quoted — and
  // calling that a disagreement between sources describes something that did
  // not happen. The data to tell them apart was already on the wire.
  const sources = new Set(slot.values.flatMap((value) => value.document_ids));
  const oneSource = sources.size === 1;

  return (
    <motion.section
      className="term"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      // Staggered, but capped: a 40-term register should not take four seconds
      // to finish arriving.
      transition={{ duration: 0.3, delay: Math.min(index * 0.025, 0.35), ease: [0.16, 1, 0.3, 1] }}
      style={active ? { background: "var(--accent-wash)", borderRadius: "var(--radius)" } : undefined}
    >
      <div className="term__head">
        <span className="term__label">{slot.label}</span>
        <span className={`pill pill--${slot.state}`}>{slot.state}</span>
        {/* Beside the state pill, not instead of it. "Settled" and "its source
         * is gone" are both true and answer different questions. */}
        {slot.orphaned && <span className="pill pill--orphaned">source removed</span>}
        {/* The one row in the register that states a human decision rather than
          * a document's words. Saying whose is the difference between a record
          * and a claim. */}
        {slot.state === "resolved" && slot.resolved_by && (
          <span className="stat">decided by {slot.resolved_by}</span>
        )}
      </div>

      {disputed ? (
        <>
          <p className="term__value term__value--muted">
            {oneSource
              ? "One document states both values — awaiting review"
              : "Sources disagree — awaiting review"}
          </p>
          <div className="options">
            {slot.values.map((value) => (
              <motion.div
                key={value.value_group}
                className="option"
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              >
                <span className="option__group">{value.value_group}</span>
                <span className="option__value">{value.text}</span>
                <span className="stat">
                  {value.citation_count} source{value.citation_count === 1 ? "" : "s"}
                </span>
              </motion.div>
            ))}
          </div>
        </>
      ) : slot.current ? (
        <p className="term__value">{slot.current.text}</p>
      ) : (
        <p className="term__value term__value--muted">Source document was removed</p>
      )}

      {/* The value above stays exactly as it was. This says the file it came
       * from is gone, which is a different claim from the value being wrong —
       * and the register never quietly drops a term a reader may have acted on.
       * `render._body` writes the same sentence into the document itself. */}
      {slot.orphaned && slot.current && (
        <p className="term__note">
          Every source for this term has been deleted from the pile folder. The value and its
          citation are kept. Removing the row is a review decision.
        </p>
      )}

      <AnimatePresence>
        {citations.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--gap-2)" }}>
            {citations.map((citation) => (
              <motion.button
                key={citation.id}
                className="cite"
                onClick={() => onCite(citation)}
                whileHover={{ y: -1 }}
                whileTap={{ scale: 0.97 }}
                title={citation.source_quote}
              >
                <span className="cite__file">{citation.filename}</span>
                <span aria-hidden>→</span>
              </motion.button>
            ))}
          </div>
        )}
      </AnimatePresence>
    </motion.section>
  );
}

/** Slots in the order the API returned them, split at each change of contract.
 * Carries the name out with the group so the caller never has to index into a
 * list TypeScript cannot promise is non-empty. */
function byAgreement(slots: Slot[]): [string, Slot[], string][] {
  const groups = new Map<string, { slots: Slot[]; name: string }>();
  for (const slot of slots) {
    const group = groups.get(slot.agreement_id) ?? {
      slots: [],
      name: slot.agreement_label,
    };
    group.slots.push(slot);
    groups.set(slot.agreement_id, group);
  }
  return [...groups].map(([id, group]) => [id, group.slots, group.name]);
}
