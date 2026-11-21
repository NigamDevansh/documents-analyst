import { useEffect, useRef, useState } from "react";

/** Who is deciding. Stored locally, sent as `X-Actor` on every decision — the
 * only authority the system has for editing a register, so it must be a real
 * name, not a default. A name like a git author; no password, because this is
 * not authentication and pretending otherwise would be worse.
 */
export function Reviewer({
  actor,
  onChange,
}: {
  actor: string | null;
  onChange: (name: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(actor ?? "");
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) input.current?.focus();
  }, [editing]);

  const commit = () => {
    const name = draft.trim();
    if (name) onChange(name);
    setEditing(false);
  };

  if (editing || !actor) {
    return (
      <div className="reviewer">
        <label className="reviewer__label" htmlFor="reviewer-name">
          Your name
        </label>
        <input
          id="reviewer-name"
          ref={input}
          className="reviewer__input"
          value={draft}
          placeholder="e.g. devansh"
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") commit();
            if (event.key === "Escape") {
              setDraft(actor ?? "");
              setEditing(false);
            }
          }}
        />
        <span className="reviewer__why">Recorded against every decision you make.</span>
      </div>
    );
  }

  return (
    <button
      className="reviewer reviewer--set"
      onClick={() => {
        setDraft(actor);
        setEditing(true);
      }}
      title="Change who decisions are recorded as"
    >
      <span className="reviewer__label">Deciding as</span>
      <span className="reviewer__name">{actor}</span>
    </button>
  );
}
