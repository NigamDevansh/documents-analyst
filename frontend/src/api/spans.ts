/** Cutting a document's text at citation offsets.
 *
 * Offsets index into `documents.extracted_text` — the exact string Gate 1
 * verified the quote against — so a highlight drawn here is showing the span
 * that was checked, not a search result. That is why this splits by index and
 * never by matching the quote text: searching would find the *first* occurrence,
 * and a contract that states "Net 30" four times would highlight the wrong one.
 */

export interface Span {
  start: number;
  end: number;
}

export interface Piece {
  text: string;
  marked: boolean;
}

/** `text` cut into plain and highlighted pieces.
 *
 * Overlapping spans are merged rather than nested — two claims citing the same
 * sentence should light it up once, and `<mark><mark>` is a DOM shape with no
 * visual meaning. Out-of-range and inverted spans are dropped: they can only
 * come from data that disagrees with itself, and drawing a highlight at a
 * clamped position would quietly point at the wrong words.
 */
export function cut(text: string, spans: Span[]): Piece[] {
  const points = Array.from(text);
  const valid = spans
    .filter((s) => s.start >= 0 && s.end <= points.length && s.end > s.start)
    .sort((a, b) => a.start - b.start);

  const merged: Span[] = [];
  for (const span of valid) {
    const last = merged[merged.length - 1];
    if (last && span.start <= last.end) last.end = Math.max(last.end, span.end);
    else merged.push({ ...span });
  }

  const pieces: Piece[] = [];
  let cursor = 0;
  for (const span of merged) {
    if (span.start > cursor) pieces.push({ text: points.slice(cursor, span.start).join(""), marked: false });
    pieces.push({ text: points.slice(span.start, span.end).join(""), marked: true });
    cursor = span.end;
  }
  if (cursor < points.length) pieces.push({ text: points.slice(cursor).join(""), marked: false });
  return pieces;
}

/** Which line a character offset falls on, 1-based — for "line 42" in the bar. */
export function lineOf(text: string, offset: number): number {
  const points = Array.from(text);
  let line = 1;
  for (let i = 0; i < Math.min(offset, points.length); i++) if (points[i] === "\n") line++;
  return line;
}
