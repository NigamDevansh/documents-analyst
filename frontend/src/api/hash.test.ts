import { describe, expect, it } from "vitest";
import { readHash } from "../App";

describe("readHash", () => {
  it("reads a pile and a tab", () => {
    expect(readHash("#pile=b08eae3f-5254-47a3-a681-063a37408dab&tab=review")).toEqual({
      pile: "b08eae3f-5254-47a3-a681-063a37408dab",
      tab: "review",
    });
  });

  it("ignores anything that is not a pile id", () => {
    // A hash is text somebody can type. `../../etc` reaching a fetch as a path
    // segment is the kind of thing that starts as a convenience feature.
    expect(readHash("#pile=../../etc/passwd").pile).toBeNull();
    expect(readHash("#pile=&tab=nonsense")).toEqual({ pile: null, tab: "register" });
    expect(readHash("").pile).toBeNull();
  });
});
