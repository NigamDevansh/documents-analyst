import { describe, expect, it } from "vitest";
import { cut, lineOf } from "./spans";

const TEXT = "Payment is due Net 30 days from the date of invoice.";

describe("cut", () => {
  it("marks exactly the span, not the first match of its text", () => {
    // "Net 30" twice: highlighting by search would light the first one.
    const twice = "Net 30 applies. The revised term is Net 30.";
    const second = twice.lastIndexOf("Net 30");
    const pieces = cut(twice, [{ start: second, end: second + 6 }]);
    expect(pieces.filter((p) => p.marked)).toEqual([{ text: "Net 30", marked: true }]);
    expect(pieces[0]!.text.startsWith("Net 30 applies")).toBe(true);
  });

  it("rebuilds the original text exactly", () => {
    const pieces = cut(TEXT, [{ start: 15, end: 21 }, { start: 0, end: 7 }]);
    expect(pieces.map((p) => p.text).join("")).toBe(TEXT);
  });

  it("merges overlapping spans instead of nesting them", () => {
    const pieces = cut(TEXT, [
      { start: 0, end: 14 },
      { start: 10, end: 21 },
    ]);
    expect(pieces.filter((p) => p.marked)).toHaveLength(1);
    expect(pieces[0]).toEqual({ text: TEXT.slice(0, 21), marked: true });
  });

  it("drops a span that cannot be drawn honestly", () => {
    // Out of range, or inverted. Clamping would highlight the wrong words while
    // looking entirely deliberate.
    expect(cut(TEXT, [{ start: 40, end: 900 }])).toEqual([{ text: TEXT, marked: false }]);
    expect(cut(TEXT, [{ start: 20, end: 5 }])).toEqual([{ text: TEXT, marked: false }]);
  });

  it("returns one plain piece when nothing is cited", () => {
    expect(cut(TEXT, [])).toEqual([{ text: TEXT, marked: false }]);
  });
});

describe("lineOf", () => {
  it("counts from one", () => {
    expect(lineOf("a\nb\nc", 0)).toBe(1);
    expect(lineOf("a\nb\nc", 2)).toBe(2);
    expect(lineOf("a\nb\nc", 4)).toBe(3);
  });
});
