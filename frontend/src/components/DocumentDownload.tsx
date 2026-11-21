import { motion } from "framer-motion";
import type { DeliverableDocument } from "../api/types";

/** The final document, as the last commit wrote it into the pile's folder.
 *
 * A plain link: the worker writes `final-document.docx` after review, so there is
 * nothing to request and nothing to wait for — the file is either there or it is
 * not yet. Before the first commit the control says so rather than vanishing,
 * because a missing button teaches nothing.
 *
 * See README.md § "The dashboard".
 */
export function DocumentDownload({
  document,
  version,
}: {
  document: DeliverableDocument;
  version: number;
}) {
  if (!document.written || !document.download) {
    return (
      <span className="export">
        <span
          className="export__note"
          role="status"
          title={`${document.filename} is written into the pile's folder once review is done`}
        >
          No final document yet
        </span>
      </span>
    );
  }

  return (
    <motion.a
      key={version}
      className="btn btn--export btn--small"
      href={document.download}
      initial={{ opacity: 0, scale: 0.97 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: "spring", stiffness: 420, damping: 30 }}
      title={document.sha256 ? `SHA-256 ${document.sha256}` : undefined}
      // The server sets `Content-Disposition`, so the browser saves it under the
      // name the pipeline gave it.
      download
    >
      <span className="export__mark" aria-hidden>↓</span>
      {document.filename}
      <span className="export__format">v{version}</span>
    </motion.a>
  );
}
