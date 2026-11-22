import { motion } from "framer-motion";
import type React from "react";
import type { Health, PileSummary } from "../api/types";

/** The pile navigator — a folder is a pile, so a list of folders. The status dot
 * is live state readable without reading: `running` pulses, `awaiting_review` is
 * amber, `parked`/`error` red. The count beside it is what is waiting for a
 * person, the only number that should make anyone click.
 */
export function Rail({
  piles,
  selected,
  onSelect,
  health,
  theme,
  onToggleTheme,
  reviewer,
  onHelp,
}: {
  piles: PileSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
  health: Health | null;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  /** Who decisions are recorded as. Passed in rather than built here so the
   * name lives with the state that sends it. */
  reviewer: React.ReactNode;
  /** Reopens the setup guide: where folders go, and what happens to them. */
  onHelp: () => void;
}) {
  return (
    <nav className="rail" id="pile-navigation" aria-label="Pile navigation">
      <div className="rail__head">
        <span className="wordmark">
          Contract Pile Analyst
          <small>{piles.length} pile{piles.length === 1 ? "" : "s"} watched</small>
        </span>
      </div>

      <div className="rail__label">Piles</div>
      <ul className="rail__list">
        {piles.map((pile) => (
          <li key={pile.id}>
            <button
              className={`pile${pile.id === selected ? " pile--selected" : ""}`}
              onClick={() => onSelect(pile.id)}
              aria-current={pile.id === selected ? "page" : undefined}
            >
              {pile.id === selected && (
                // One element that slides between items rather than a border
                // that blinks on and off — the movement is what tells you the
                // selection went somewhere.
                <motion.span
                  layoutId="pile-selection"
                  className="pile__glow"
                  transition={{ type: "spring", stiffness: 500, damping: 40 }}
                />
              )}
              <span
                className={`dot dot--${pile.status}${pile.status === "running" ? " dot--pulse" : ""}`}
                title={pile.status}
              />
              <span className="pile__name">{pile.name}</span>
              {pile.pending_decisions > 0 && (
                <motion.span
                  className="tally"
                  initial={{ scale: 0.6, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  transition={{ type: "spring", stiffness: 500, damping: 30 }}
                >
                  {pile.pending_decisions}
                </motion.span>
              )}
              <span className="pile__meta">
                {pile.documents} doc{pile.documents === 1 ? "" : "s"}
                {pile.open_conflicts > 0 && ` · ${pile.open_conflicts} disputed`}
                {pile.quarantined > 0 && ` · ${pile.quarantined} quarantined`}
              </span>
            </button>
          </li>
        ))}
        {piles.length === 0 && (
          <li className="stat" style={{ padding: "var(--gap-3)" }}>
            Drop a folder into <code>{health?.watch?.path ?? "watched/"}</code> — it becomes a
            pile with no setup step.
          </li>
        )}
      </ul>

      <div className="rail__reviewer">{reviewer}</div>

      <div className="rail__foot">
        <span title={health?.degraded ? health.missing_configuration.join("\n") : "fully configured"}>
          <span
            className={`dot dot--${health?.degraded ? "awaiting_review" : "running"}`}
            style={{ display: "inline-block", marginRight: 6 }}
          />
          {health ? (health.degraded ? "degraded" : "ready") : "…"}
        </span>
        <button className="btn btn--ghost" onClick={onHelp} aria-label="Show the setup guide">
          ?
        </button>
        <button
          className="btn btn--ghost"
          onClick={onToggleTheme}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          {theme === "dark" ? "☾" : "☀"}
        </button>
      </div>
    </nav>
  );
}
