import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Health } from "../api/types";
import { KeyRequirements } from "./KeyRequirements";

const POLL_MS = 4000;

/** The whole app until the deployment can actually analyse something.
 *
 * Without it a missing key let people browse, drop a folder in, and wait for an
 * answer that was never coming — the worker reads files but runs nothing without an
 * LLM key and an embedding key. So nothing else renders: this screen says what to
 * add and where, re-checks `/health` every few seconds, and gets out of the way by
 * itself once `setup.ready` is true (after `.env` is saved and `make up` re-run).
 */
export function SetupGate({ health, onReady }: { health: Health | null; onReady: (h: Health) => void }) {
  const [checking, setChecking] = useState(false);
  const [unreachable, setUnreachable] = useState(false);

  const check = async () => {
    setChecking(true);
    try {
      const next = await api.health();
      setUnreachable(false);
      onReady(next);
    } catch {
      setUnreachable(true);
    } finally {
      setChecking(false);
    }
  };

  useEffect(() => {
    const timer = window.setInterval(() => {
      check().catch(() => undefined);
    }, POLL_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const requirements = health?.setup?.requirements ?? [];
  const missing = requirements.filter((r) => !r.met).length;

  return (
    <div className="gate" role="dialog" aria-modal="true" aria-labelledby="gate-title">
      <div className="gate__card">
        <p className="gate__eyebrow">Document Analyst · setup</p>
        <h1 id="gate-title">
          {missing === 1 ? "One more key before you start" : "Add your API keys to start"}
        </h1>
        <p className="gate__lede">
          Nothing can be analysed until both of these are in place — without them files are
          found and read, then wait. This page unlocks by itself once they are.
        </p>

        {unreachable && (
          <p className="gate__warn" role="alert">
            Can't reach the API at the moment — is the stack running? (<code>make up</code>)
          </p>
        )}

        {requirements.length > 0 ? (
          <KeyRequirements requirements={requirements} />
        ) : (
          <p className="gate__warn">Asking the API which keys are set…</p>
        )}

        <h2 className="gate__h2">How to add them</h2>
        <ol className="gate__steps">
          <li>
            Open <code>.env</code> in the repo folder. <code>make up</code> creates it from{" "}
            <code>.env.example</code>; or run <code>cp .env.example .env</code>.
          </li>
          <li>
            Put each key on its own line as <code>NAME=value</code> — no quotes, no spaces —
            for example <code>OPENAI_API_KEY=sk-…</code>. One OpenAI key, or one Gemini key,
            covers both requirements. With Claude (<code>ANTHROPIC_API_KEY</code>) also add
            an OpenAI or Gemini key: Claude has no embedding model.
          </li>
          <li>
            Save, then run <code>make up</code> again so the containers read the new values.
          </li>
        </ol>

        <div className="gate__foot">
          <span className="gate__poll" aria-live="polite">
            {checking ? "Checking…" : `Checking again every ${POLL_MS / 1000} s`}
          </span>
          <button className="btn btn--primary" onClick={() => check()} disabled={checking}>
            Check again
          </button>
        </div>
      </div>
    </div>
  );
}
