import { motion } from "framer-motion";
import { useEffect, useRef } from "react";
import { cut, lineOf } from "../api/spans";
import type { Citation, DocumentText } from "../api/types";

/** The contract, with the cited span lit up. A plain click handler, no iframe:
 * we render the source and our own text in the same DOM, same origin. The
 * highlight is drawn by offset, never by searching for the quote — a contract
 * that says "Net 30" four times would otherwise light the wrong one.
 *
 * See docs/UI_AND_LINKING.html.
 */
export function SourcePane({
  document: doc,
  citation,
  loading,
  focus,
}: {
  document: DocumentText | null;
  citation: Citation | null;
  loading: boolean;
  /** Counts citation clicks. Clicking the pill for the citation already open is
   * a real request — "take me back there" — and it changes nothing else here,
   * so without this the second click on a pill did nothing at all. */
  focus: number;
}) {
  const markRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    // Nothing to scroll to while the skeleton is up. The mark does not exist
    // yet, and this used to fire anyway — on the previous document's node,
    // already detached, which scrolls nothing — and then never fire again,
    // because by the time the text arrived neither the citation id nor the
    // document id had changed. That is how a second citation in a file already
    // open left you sitting at the top of it.
    if (loading || !citation) return;
    const node = markRef.current;
    if (!node) return;
    // After paint, so the element has been laid out and the smooth scroll has
    // somewhere to go.
    const id = requestAnimationFrame(() =>
      node.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" }),
    );
    return () => cancelAnimationFrame(id);
  }, [citation?.id, doc?.id, loading, focus]);

  if (loading) {
    return (
      <div className="source">
        <div className="source__bar">
          <div className="skeleton" style={{ height: 14, width: 160 }} />
        </div>
        <div className="source__body" style={{ display: "grid", gap: 10 }}>
          {Array.from({ length: 12 }, (_, i) => (
            <div key={i} className="skeleton" style={{ height: 12, width: `${92 - i * 3}%` }} />
          ))}
        </div>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="source">
        <div className="source__bar">Source</div>
        <div className="empty">
          <h3>Nothing selected</h3>
          <p>
            Click a citation beside any term and the sentence it came from appears here, at the
            exact offsets that were verified when it was extracted.
          </p>
        </div>
      </div>
    );
  }

  const pieces = citation
    ? cut(doc.text, [{ start: citation.char_start, end: citation.char_end }])
    : cut(doc.text, []);

  return (
    <div className="source">
      <div className="source__bar">
        <span className="source__file">{doc.filename}</span>
        {citation && (
          <motion.span
            key={citation.id}
            className="stat"
            initial={{ opacity: 0, x: -4 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.2 }}
          >
            line {lineOf(doc.text, citation.char_start)} · chars {citation.char_start}–
            {citation.char_end}
          </motion.span>
        )}
        <span className="spacer" />
        <span className="stat">{doc.length.toLocaleString()} chars</span>
      </div>
      <pre className="source__body">
        {pieces.map((piece, index) =>
          piece.marked ? (
            <mark
              className="span"
              key={index}
              // Assigned, not guarded with `if (node)`. Keeping the old node on
              // unmount left this pointing at detached text from the last
              // document, which scrolls silently and looks like nothing broke.
              ref={(node) => {
                markRef.current = node;
              }}
            >
              {piece.text}
            </mark>
          ) : (
            <span key={index}>{piece.text}</span>
          ),
        )}
      </pre>
    </div>
  );
}
