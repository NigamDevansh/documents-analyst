import { motion } from "framer-motion";
import { useEffect, useRef } from "react";
import type { Health } from "../api/types";
import { KeyRequirements } from "./KeyRequirements";

/** How to use this, said once on first load and again whenever there is nothing to
 * show: where pile folders go (the real path, from `/health`), which keys are
 * missing, how files and folders are processed, and where the result lands.
 *
 * Two shapes, one text. `modal` is the first-load overlay, remembered per reviewer;
 * `inline` fills the workspace while no folder has been found yet, because that is
 * exactly when someone needs to know where to put one.
 *
 * See README.md § "Quick start".
 */
export function SetupGuide({
  health,
  variant,
  onDismiss,
}: {
  health: Health | null;
  variant: "modal" | "inline";
  onDismiss?: () => void;
}) {
  // Focus the button for the keyboard, but without `autoFocus`: focusing an element at
  // the bottom of a scrolling card scrolls the card to it, so the guide opened on its
  // last step and the keys at the top were never seen.
  const done = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    done.current?.focus({ preventScroll: true });
  }, []);

  const watch = health?.watch;
  const root = watch?.path ?? "./watched";
  const shown = root.replace(/\/+$/, "");
  const filename = watch?.deliverable_filename ?? "final-document.docx";
  const lanes = watch?.pile_concurrency ?? 1;
  const requirements = health?.setup?.requirements ?? [];
  const insideRepo = shown === "./watched" || shown === "watched";

  const body = (
    <>
      <h2 id="setup-title">Point it at a folder of documents</h2>
      <p className="welcome__lede">
        Every folder you put in the watched folder becomes a <b>pile</b>: its files are read,
        checked against each other and your rulebook, put to you for review, and turned into{" "}
        <code>{filename}</code> inside that same folder.
      </p>

      <ol className="welcome__list">
        <li>
          <b>API keys, in <code>.env</code></b> (in the repo folder) as <code>NAME=value</code>,
          then <code>make up</code> again. Two things are needed:
          {requirements.length > 0 ? (
            <KeyRequirements requirements={requirements} />
          ) : (
            <span className="setup__note">Could not ask the API which keys are set.</span>
          )}
        </li>
        <li>
          <b>Put each pile in its own folder, inside</b>
          <pre className="setup__path">
            {shown}/{"\n"}
            {"  "}acme/{"            "}← one pile{"\n"}
            {"    "}msa.pdf{"\n"}
            {"    "}amendment.docx{"\n"}
            {"    "}rulebook.yaml{"  "}← optional policy checks{"\n"}
            {"  "}meridian-health/{" "}← another pile
          </pre>
          {insideRepo ? (
            <>
              That is the <code>watched/</code> folder in this repo. Copy folders into it. To
              watch a folder somewhere else on your machine instead, set{" "}
              <code>WATCH_DIR=/absolute/path</code> in <code>.env</code> and run{" "}
              <code>make up</code> again.
            </>
          ) : (
            <>
              Set by <code>WATCH_DIR</code> in <code>.env</code>. Change it and run{" "}
              <code>make up</code> again to watch another folder.
            </>
          )}{" "}
          Supported files: PDF, DOCX, Markdown, TXT, HTML. A new folder is found within{" "}
          {watch?.scan_interval_s ?? 30}s; no other setup step.
        </li>
        <li>
          <b>How it runs.</b> Inside a pile the files are read and extracted one at a time, in
          order, then cross-checked together — a conflict needs two files to exist. Separate
          folders run side by side, up to {lanes} at once
          {lanes === 1 ? " (one after another)" : ", one thread each"}.
        </li>
        <li>
          <b>Review.</b> Put your name in the sidebar — every decision is recorded against it.
          The <b>Review</b> tab lists the conflicts and rulebook findings; pick the value that
          wins and approve, or reject. Click any citation to see the sentence it came from.
        </li>
        <li>
          <b>The final document</b> is written to{" "}
          <code>
            {shown}/&lt;pile&gt;/{filename}
          </code>{" "}
          as soon as the queue is empty, and rewritten when a file changes. Download it top
          right; its history is kept per section.
        </li>
      </ol>
    </>
  );

  if (variant === "inline") {
    return (
      <div className="setup setup--inline" aria-labelledby="setup-title">
        <div className="welcome__card setup__card">{body}</div>
      </div>
    );
  }

  return (
    <motion.div
      className="welcome"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2 }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="setup-title"
    >
      <motion.div
        className="welcome__card"
        initial={{ y: 14, scale: 0.98 }}
        animate={{ y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
      >
        {body}
        <div className="welcome__foot">
          <button className="btn btn--primary" onClick={onDismiss} ref={done}>
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
