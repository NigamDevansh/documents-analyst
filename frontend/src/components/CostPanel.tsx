import { motion } from "framer-motion";
import type { Cost, Finding } from "../api/types";

/** Milliseconds as something a person reads: `840ms`, `9.2s`, `3m 04s`.

 * Shown rather than raw ms because "where did the run go" is a question about
 * minutes, and `184,532` is a number you have to do arithmetic on first. */
function duration(ms: number): string {
  if (!ms) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${String(Math.round((ms % 60_000) / 1000)).padStart(2, "0")}s`;
}

/** What this pile cost, and what it found. Failed calls are included, and a model
 * with no price entry shows as unpriced, never free. Writing the final document is
 * local file work and costs nothing, so only model calls appear here.
 *
 * See README.md § "The dashboard".
 */
export function CostPanel({ cost, findings }: { cost: Cost | null; findings: Finding[] }) {
  if (!cost) return <div className="stack"><div className="skeleton" style={{ height: 160 }} /></div>;

  const spend = Number(cost.cost_usd ?? 0);
  const unpriced = spend === 0 && cost.calls > 0;

  return (
    <div className="stack">
      <div className="grid-2">
        <Metric
          label="LLM spend"
          value={unpriced ? "unpriced" : `$${spend.toFixed(4)}`}
        >
          <span className="stat">
            {unpriced
              ? `${cost.calls} calls, no rates configured — never reported as free`
              : `${cost.calls} calls`}
          </span>
        </Metric>

        <Metric label="Time in models" value={duration(cost.latency_ms)}>
          <span className="stat">
            wall time across every call, retries included — the stage table below says where
          </span>
        </Metric>

        <Metric label="Failed calls" value={String(cost.failures)}>
          <span className="stat">counted, because a retry still spent the first attempt</span>
        </Metric>

        <Metric label="Open findings" value={String(findings.length)}>
          <span className="stat">
            rulebook verdicts and document scans still standing
          </span>
        </Metric>
      </div>

      {cost.by_node.length > 0 && (
        <div className="card">
          <div className="card__head">
            <span className="card__kind">By stage</span>
          </div>
          <div className="card__body">
            <table className="plain">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Calls</th>
                  <th>Tokens in</th>
                  <th>Tokens out</th>
                  <th>Time</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {cost.by_node.map((row) => (
                  <tr key={row.node}>
                    <td>{row.node}</td>
                    <td>{row.calls}</td>
                    <td>{row.tokens_in.toLocaleString()}</td>
                    <td>{row.tokens_out.toLocaleString()}</td>
                    <td>{duration(row.latency_ms)}</td>
                    <td>{Number(row.cost_usd) === 0 ? "—" : `$${Number(row.cost_usd).toFixed(4)}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {findings.length > 0 && (
        <div className="card">
          <div className="card__head">
            <span className="card__kind">Findings</span>
          </div>
          <div className="card__body">
            {findings.map((finding) => (
              <div key={finding.id} style={{ display: "grid", gap: 4 }}>
                <div style={{ display: "flex", gap: "var(--gap-3)", alignItems: "baseline" }}>
                  <span className="option__group">{finding.rule_id}</span>
                  <span className={`pill pill--${finding.severity === "high" ? "disputed" : "settled"}`}>
                    {finding.severity}
                  </span>
                  <span className="stat">{finding.evaluated_by}</span>
                </div>
                <p style={{ margin: 0 }}>{finding.message}</p>
                {/* The sentence itself, for a `scan` finding especially: being
                  * told a document gave orders is not the same as reading what
                  * it said, and this system quotes rather than characterises. */}
                {finding.citations?.slice(0, 3).map((citation, index) =>
                  citation.quote ? (
                    <blockquote className="quote" key={index}>
                      “{citation.quote.trim()}”
                    </blockquote>
                  ) : null,
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  children,
}: {
  label: string;
  value: string;
  children?: React.ReactNode;
}) {
  return (
    <motion.div
      className="card"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="card__body">
        <span className="card__kind">{label}</span>
        <span style={{ font: "600 var(--step-3)/1.1 var(--serif)", letterSpacing: "-0.02em" }}>
          {value}
        </span>
        {children}
      </div>
    </motion.div>
  );
}
