import type { KeyRequirement } from "../api/types";

const LABEL: Record<string, string> = {
  anthropic: "Claude",
  openai: "OpenAI",
  gemini: "Gemini",
};

/** The two things every deployment needs — an LLM key and an embedding key — each met
 * by any one of the keys listed under it. Shared by the blocking setup screen and the
 * setup guide so the two can never disagree about what is required. */
export function KeyRequirements({ requirements }: { requirements: KeyRequirement[] }) {
  return (
    <div className="keyreqs">
      {requirements.map((req, i) => (
        <section key={req.id} className={`keyreq keyreq--${req.met ? "met" : "missing"}`}>
          <header className="keyreq__head">
            <span className="keyreq__n">{i + 1}</span>
            <span className="keyreq__title">
              {req.title} <span className="keyreq__purpose">— {req.purpose}</span>
            </span>
            <span className={`pill pill--${req.met ? "settled" : "disputed"}`}>
              {req.met ? "done" : "needed"}
            </span>
          </header>
          <p className="keyreq__any">
            {req.any_of.length > 1 ? "Add any one of:" : "Add:"}
          </p>
          <ul className="keyreq__options">
            {req.any_of.map((opt) => (
              <li key={opt.name}>
                <code>{opt.name}</code>
                <span className="keyreq__label">{opt.label}</span>
                {/* Once one key meets the requirement, the others are alternatives,
                 * not gaps — a red "missing" beside them read as a problem. */}
                <span
                  className={`pill pill--${opt.set ? "settled" : req.met ? "neutral" : "disputed"}`}
                >
                  {opt.set ? "set" : req.met ? "not needed" : "missing"}
                </span>
              </li>
            ))}
          </ul>
          {req.met && (
            <p className="keyreq__using">
              Using <b>{LABEL[req.using] ?? req.using}</b>
              {req.models.filter(Boolean).length > 0 && (
                <>
                  {" "}
                  (<code>{[...new Set(req.models.filter(Boolean))].join(" / ")}</code>)
                </>
              )}
              {req.chosen_by === "auto" ? ", picked from the keys you set." : `, set by ${req.chosen_by}.`}
            </p>
          )}
        </section>
      ))}
    </div>
  );
}
