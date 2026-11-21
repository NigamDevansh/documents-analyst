import { motion } from "framer-motion";

/** The four things that are not discoverable, said once: the name field looks
 * optional and is not, the final document's download is a small corner control, and the tabs give no
 * hint that Review is where the work is. Shown once per reviewer, keyed to the
 * name (a shared machine has more than one).
 */
export function Welcome({ onDismiss }: { onDismiss: () => void }) {
  return (
    <motion.div
      className="welcome"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2 }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="welcome-title"
    >
      <motion.div
        className="welcome__card"
        initial={{ y: 14, scale: 0.98 }}
        animate={{ y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
      >
        <h2 id="welcome-title">A folder of contracts, read and cross-checked</h2>
        <p className="welcome__lede">
          Every value below carries a citation checked against the document it came from. Four
          things worth knowing before you start.
        </p>

        <ol className="welcome__list">
          <li>
            <b>Put your name in the sidebar.</b> Every approval is recorded against it — it is the
            only authority for editing the register, so decisions are refused without one.
          </li>
          <li>
            <b>Review is where the work is.</b> The tabs are the register as it stands, the
            questions waiting on you, and what the run cost. The count on Review is how many.
          </li>
          <li>
            <b>Click any citation</b> and the contract opens beside it with the exact sentence
            highlighted. Nothing in the register is stated without one.
          </li>
          <li>
            <b>The final document</b> is written into the pile's folder as{" "}
            <code>final-document.docx</code> once your decisions are applied — download it top
            right.
          </li>
        </ol>

        <div className="welcome__foot">
          <button className="btn btn--primary" onClick={onDismiss} autoFocus>
            Got it
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

/** Remembered per reviewer, so a second person on the same machine still sees it. */
export function welcomeKey(actor: string | null): string {
  return `welcome:${actor ?? "anonymous"}`;
}
