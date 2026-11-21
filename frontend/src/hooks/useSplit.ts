import { useCallback, useEffect, useRef, useState } from "react";

const KEY = "split";

/** A draggable divider that remembers where you left it. Stored as a fraction,
 * not pixels, so it survives a resize (620px is generous on a laptop, a sliver
 * on a monitor). Pointer events, so trackpad/touch/pen all work, and
 * `setPointerCapture` keeps the drag alive when the cursor outruns the handle.
 */
export function useSplit(initial = 0.56) {
  const [fraction, setFraction] = useState(() => {
    const saved = Number(localStorage.getItem(KEY));
    return Number.isFinite(saved) && saved > 0.15 && saved < 0.85 ? saved : initial;
  });
  const [dragging, setDragging] = useState(false);
  const container = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    localStorage.setItem(KEY, String(fraction));
  }, [fraction]);

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  }, []);

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      if (!dragging || !container.current) return;
      const box = container.current.getBoundingClientRect();
      setFraction(clamp((event.clientX - box.left) / box.width));
    },
    [dragging],
  );

  const stop = useCallback(() => setDragging(false), []);

  /** Keyboard is not an afterthought: a divider only reachable by pointer is a
   * layout some people cannot change. */
  const onKeyDown = useCallback((event: React.KeyboardEvent<HTMLButtonElement>) => {
    const step = event.shiftKey ? 0.1 : 0.02;
    if (event.key === "ArrowLeft") setFraction((f) => clamp(f - step));
    else if (event.key === "ArrowRight") setFraction((f) => clamp(f + step));
    else if (event.key === "Home") setFraction(0.35);
    else if (event.key === "End") setFraction(0.75);
    else return;
    event.preventDefault();
  }, []);

  return {
    fraction,
    dragging,
    container,
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: stop,
      onPointerCancel: stop,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-valuenow": Math.round(fraction * 100),
      tabIndex: 0,
      "data-dragging": dragging,
    },
  };
}

const clamp = (value: number, min = 0.2, max = 0.8) =>
  Math.min(max, Math.max(min, value));
