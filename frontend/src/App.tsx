import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api/client";
import { changedSomething, highestId, unhandled } from "./api/refresh";
import type {
  Citation,
  Cost,
  DecisionItem,
  DocumentText,
  Finding,
  Health,
  PileSummary,
  Register as RegisterData,
} from "./api/types";
import { CostPanel } from "./components/CostPanel";
import { DocumentDownload } from "./components/DocumentDownload";
import { Rail } from "./components/Rail";
import { Register } from "./components/Register";
import { Reviewer } from "./components/Reviewer";
import { ReviewQueue } from "./components/ReviewQueue";
import { SourcePane } from "./components/SourcePane";
import { Activity } from "./components/Activity";
import { SetupGate } from "./components/SetupGate";
import { SetupGuide, welcomeKey } from "./components/SetupGuide";
import { useEvents } from "./hooks/useEvents";
import { useSplit } from "./hooks/useSplit";

type Tab = "register" | "review" | "cost";

/** `#pile=<uuid>&tab=review` → `{ pile, tab }`. Anything unrecognised is
 * ignored rather than trusted: a hash is user-editable text, and a pile id from
 * it goes straight into a request path. */
export function readHash(hash: string = location.hash): { pile: string | null; tab: Tab } {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const pile = params.get("pile");
  const tab = params.get("tab");
  return {
    pile: pile && /^[0-9a-f-]{36}$/i.test(pile) ? pile : null,
    tab: tab === "review" || tab === "cost" ? tab : "register",
  };
}

/** The workspace. One pile at a time. Reloading is driven by the event stream —
 * a stage event means something changed, so that is when to ask what. The stream
 * cannot say a *different* pile started (it is subscribed to the one on screen),
 * so the pile list, and only that, is also polled every few seconds.
 *
 * See README.md § "The dashboard".
 */
export default function App() {
  const [piles, setPiles] = useState<PileSummary[]>([]);
  // Distinguishes "no folders yet" from "not asked yet", so the setup guide does
  // not flash over a workspace that is about to load.
  const [pilesLoaded, setPilesLoaded] = useState(false);
  // The hash, not a router. `#pile=<id>&tab=review` makes a specific pile a
  // shareable link — the property the architecture asks for — without adding a
  // routing library to a single-screen app. It also survives a reload, which is
  // what somebody debugging a pile actually wants.
  const [pileId, setPileId] = useState<string | null>(() => readHash().pile);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<Tab>(() => readHash().tab);
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (document.documentElement.dataset.theme as "light" | "dark") ?? "light",
  );

  const [register, setRegister] = useState<RegisterData | null>(null);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [queue, setQueue] = useState<DecisionItem[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [cost, setCost] = useState<Cost | null>(null);

  const [citation, setCitation] = useState<Citation | null>(null);
  const [doc, setDoc] = useState<DocumentText | null>(null);
  const [loadingDoc, setLoadingDoc] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  /** A load failure that is still true. A toast says it once and disappears;
   * this stays until a retry succeeds, because the screen is still wrong. */
  const [broken, setBroken] = useState<string | null>(null);
  const [toast, setToast] = useState<{ text: string; bad?: boolean } | null>(null);

  const { events, live } = useEvents(pileId);
  const split = useSplit();

  // Null until somebody says who they are. NOT defaulted to "reviewer": that
  // string went into `decided_by` on every approval, and that row is the only
  // authority the system has for editing a contract register. An audit trail
  // naming a person who does not exist is worse than one that refuses to
  // record until it knows.
  const [actor, setActor] = useState<string | null>(() => localStorage.getItem("actor"));
  const nameReviewer = useCallback((name: string) => {
    // Carry the tour dismissal from `anonymous` to the name just given.
    // Without this the sequence that actually happens — read the tour, dismiss
    // it, then type your name because the tour told you to — shows the tour
    // again on the next load, under a key that had never seen it.
    const previous = welcomeKey(localStorage.getItem("actor"));
    if (localStorage.getItem(previous) === "done") {
      localStorage.setItem(welcomeKey(name), "done");
    }
    localStorage.setItem("actor", name);
    setActor(name);
  }, []);

  // Shown once per reviewer, and again from the "?" in the sidebar: where pile
  // folders go, which keys are missing, and how a folder turns into a document —
  // none of which this screen can show until a folder has been found.
  const [welcomed, setWelcomed] = useState(
    () => localStorage.getItem(welcomeKey(localStorage.getItem("actor"))) === "done",
  );
  const dismissWelcome = useCallback(() => {
    localStorage.setItem(welcomeKey(localStorage.getItem("actor")), "done");
    setWelcomed(true);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  }, [theme]);

  useEffect(() => {
    if (!pileId) return;
    // `replaceState`, not `location.hash =` — the latter pushes a history entry
    // per click, so Back would walk through every tab somebody looked at
    // instead of leaving the app.
    history.replaceState(null, "", `#pile=${pileId}&tab=${tab}`);
  }, [pileId, tab]);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  // A hash edited by hand (or a link followed inside the open tab) changes no
  // state on its own: without this, `#…&tab=review` showed the Register.
  useEffect(() => {
    const follow = () => {
      const next = readHash();
      setTab(next.tab);
      if (next.pile) setPileId(next.pile);
    };
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, []);

  const loadPiles = useCallback(async () => {
    const list = await api.piles();
    setPiles(list);
    setPilesLoaded(true);
    setPileId((current) => current ?? list[0]?.id ?? null);
  }, []);

  useEffect(() => {
    loadPiles().catch((error: Error) => setToast({ text: error.message, bad: true }));
  }, [loadPiles]);

  // `allSettled`, not `all`. Five independent reads were tied together so that
  // one failure discarded the other four and left an almost empty workspace
  // behind a toast that vanished in four seconds. They are independent — a
  // costing query that fails has nothing to say about the register — so each
  // one lands on its own and whatever failed is named and retryable.
  const loadPile = useCallback(async (id: string) => {
    const results = await Promise.allSettled([
      api.register(id),
      api.citations(id),
      api.queue(id),
      api.findings(id),
      api.cost(id),
    ]);
    const [registerData, citationList, items, findingList, costData] = results;
    if (registerData.status === "fulfilled") setRegister(registerData.value);
    if (citationList.status === "fulfilled") setCitations(citationList.value);
    if (items.status === "fulfilled") setQueue(items.value);
    if (findingList.status === "fulfilled") setFindings(findingList.value);
    if (costData.status === "fulfilled") setCost(costData.value);

    const names = ["register", "citations", "review queue", "findings", "costs"];
    const failed = results
      .map((result, index) => (result.status === "rejected" ? names[index] : null))
      .filter((name): name is string => name !== null);
    setBroken(
      failed.length === 0
        ? null
        : `Could not load the ${failed.join(", ")}. Everything else on this page is current.`,
    );
  }, []);

  useEffect(() => {
    if (!pileId) return;
    setCitation(null);
    setDoc(null);
    loadPile(pileId).catch((error: Error) => setToast({ text: error.message, bad: true }));
  }, [pileId, loadPile]);

  // A stage event means the graph changed something. Refetch on the ones that
  // do, and ignore the ones that only say it is still thinking.
  //
  // **Every new event is examined, not just the newest one.** A stream that has
  // never connected replays the pile's whole history in a single chunk, React
  // renders that burst once, and whatever frame happens to be last in it — very
  // often a `stage.extract.document` — decided the whole question. Opening a
  // running pile therefore showed it as `idle` with a register that was already
  // being written.
  const handled = useRef(0);
  useEffect(() => {
    handled.current = 0;
  }, [pileId]);
  useEffect(() => {
    if (!pileId) return;
    const fresh = unhandled(events, handled.current);
    if (fresh.length === 0) return;
    handled.current = highestId(fresh, handled.current);
    // The pile list is one small query and `status` is what the sidebar dot and
    // the header pill are drawn from, so any event at all earns it. The five
    // per-pile reads do not: those wait for a stage that changed something.
    loadPiles().catch(() => undefined);
    if (changedSomething(fresh)) loadPile(pileId).catch(() => undefined);
  }, [events, pileId, loadPile, loadPiles]);

  // The event stream is scoped to the pile on screen, so nothing on this page
  // hears that a *different* pile has been picked up — and the sidebar is
  // showing all of them. One small list every few seconds, and only while the
  // tab is actually in front of somebody.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") loadPiles().catch(() => undefined);
    };
    const id = setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [loadPiles]);

  // Bumped on every citation click, including a click on the one already open.
  // The source pane scrolls when something it watches changes, and clicking the
  // same pill twice changes none of it — so the second click did nothing at all.
  const [focus, setFocus] = useState(0);
  const showCitation = useCallback(
    async (next: Citation) => {
      setCitation(next);
      setFocus((n) => n + 1);
      // Already open. Refetching text we are holding swaps the pane for a
      // skeleton and back, and a skeleton has no highlight to scroll to — which
      // is how a second citation in the *same* file ended up at the top of an
      // unscrolled document.
      if (doc?.id === next.source_document_id) return;
      setLoadingDoc(true);
      try {
        setDoc(await api.documentText(next.source_document_id));
      } catch (error) {
        setToast({ text: (error as Error).message, bad: true });
      } finally {
        setLoadingDoc(false);
      }
    },
    [doc?.id],
  );

  const decide = useCallback(
    async (item: DecisionItem, approve: boolean, group?: number) => {
      if (!actor) {
        // Refused here as well as by the API, because the useful moment to say
        // it is before the click, not after a 422.
        setToast({ text: "Add your name in the sidebar before deciding", bad: true });
        return;
      }
      setBusy(item.id);
      try {
        const result = await api.decide(item.id, approve, actor, {
          chosen_value_group: group,
        });
        setQueue((current) => current.filter((row) => row.id !== item.id));
        setToast({
          text: `${approve ? "Approved" : "Rejected"} as ${result.decision.decided_by} — the worker commits on its next pass`,
        });
        if (pileId) {
          loadPile(pileId).catch(() => undefined);
          loadPiles().catch(() => undefined);
        }
      } catch (error) {
        // The API's refusals are written to be read: "approving a conflict
        // requires chosen_value_group", "already approved by devansh". Show
        // them as they are.
        const message =
          error instanceof ApiError ? error.message : (error as Error).message;
        setToast({ text: message, bad: true });
      } finally {
        setBusy(null);
      }
    },
    [actor, pileId, loadPile, loadPiles],
  );

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), toast.bad ? 7000 : 4000);
    return () => clearTimeout(id);
  }, [toast]);

  const pile = piles.find((row) => row.id === pileId) ?? null;

  // Missing keys block everything: a dashboard you can click around in while nothing
  // can ever be analysed only teaches people to wait for an answer that isn't coming.
  if (health?.setup && !health.setup.ready) {
    return <SetupGate health={health} onReady={setHealth} />;
  }

  return (
    <div className="shell">
      <Rail
        piles={piles}
        selected={pileId}
        onSelect={setPileId}
        health={health}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        reviewer={<Reviewer actor={actor} onChange={nameReviewer} />}
        onHelp={() => setWelcomed(false)}
      />

      <main className="main">
        <header className="topbar">
          <h1>{pile?.name ?? "…"}</h1>
          {register && <span className="stat">document <b>v{register.version}</b></span>}
          {pile && <span className="stat"><b>{pile.documents}</b> documents</span>}
          {pile && pile.open_conflicts > 0 && (
            <span className="stat"><b>{pile.open_conflicts}</b> disputed</span>
          )}
          {/* Optional by design, invisible by accident. Without this, a pile
            * with no rulebook shows zero findings and reads as compliant. */}
          {pile && !pile.has_rulebook && (
            <span className="rulebook-notice" role="status">
              <span className="rulebook-notice__icon" aria-hidden>!</span>
              <span>
                <strong>Compliance checks are off</strong>
                <small>Add <code>rulebook.yaml</code> to this pile to check it against your policies.</small>
              </span>
            </span>
          )}
          <span className="topbar__spacer" />
          {pile && register && (
            <DocumentDownload document={register.document} version={register.version} />
          )}
          {pile && <span className={`pill pill--${pile.status === "awaiting_review" ? "disputed" : "settled"}`}>{pile.status.replace("_", " ")}</span>}
        </header>

        {/* **The first thing a stranger sees, and it was a tooltip.**
          * `/health` has always named every missing key and what each one
          * costs; the only place it appeared was the title attribute on the
          * word "degraded" at the bottom of the sidebar. Somebody who has just
          * cloned the repo and is wondering why nothing is being extracted
          * does not hover a small grey word — so the list is on the page, in
          * the order the run needs them. */}
        {health?.degraded && health.missing_configuration.length > 0 && (
          <div className="banner banner--setup" role="status">
            <span>
              <b>Running without credentials.</b> Everything that needs no key works — the
              folder is watched, documents are read, hashed and quarantined. Add these to{" "}
              <code>.env</code> and the same piles carry on:
              <br />
              {health.missing_configuration.map((line) => (
                <span key={line} className="banner__line">
                  {line}
                </span>
              ))}
            </span>
          </div>
        )}

        {broken && (
          <div className="banner" role="alert">
            <span>{broken}</span>
            <button
              className="btn btn--ghost btn--small"
              onClick={() => {
                if (pileId) loadPile(pileId).catch(() => undefined);
              }}
            >
              Retry
            </button>
          </div>
        )}

        {pilesLoaded && piles.length === 0 ? (
          <SetupGuide health={health} variant="inline" />
        ) : (
          <>
        <div className="tabs" role="tablist">
          {(
            [
              ["register", "Register", register?.slots.length],
              ["review", "Review", queue.length || undefined],
              ["cost", "Cost & findings", undefined],
            ] as const
          ).map(([key, label, count]) => (
            <button
              key={key}
              className="tab"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
            >
              {label}
              {count !== undefined && <span className="tab__count">{count}</span>}
              {tab === key && (
                <motion.span
                  layoutId="tab-rule"
                  className="tab__rule"
                  transition={{ type: "spring", stiffness: 520, damping: 40 }}
                />
              )}
            </button>
          ))}
        </div>

        {/* The split container is deliberately NOT keyed by tab. It owns the
         * ref the drag measures against, and an element that unmounts when you
         * change tab measures nothing on the way back. The panel inside is what
         * animates. */}
        <div className="split" ref={split.container}>
          {tab === "register" && register && (
            <>
              <motion.div
                key="register"
                className="split__pane"
                style={{ flexBasis: `${split.fraction * 100}%` }}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.2 }}
              >
                <Register
                  data={register}
                  citations={citations}
                  onCite={showCitation}
                  activeSlot={citation?.slot_id ?? null}
                  waitingForKeys={
                    health?.missing_configuration.some((line) =>
                      line.includes("fact extraction"),
                    ) ?? false
                  }
                />
              </motion.div>
              <button className="split__handle" {...split.handleProps} aria-label="Resize panes" />
              <div className="split__pane" style={{ flex: 1 }}>
                <SourcePane
                  document={doc}
                  citation={citation}
                  loading={loadingDoc}
                  focus={focus}
                />
              </div>
            </>
          )}

          {tab === "review" && (
            <motion.div
              key="review"
              className="split__pane"
              style={{ flex: 1 }}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
            >
              <ReviewQueue items={queue} onDecide={decide} busy={busy} />
            </motion.div>
          )}

          {tab === "cost" && (
            <motion.div
              key="cost"
              className="split__pane"
              style={{ flex: 1 }}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
            >
              <CostPanel cost={cost} findings={findings} />
            </motion.div>
          )}
        </div>
          </>
        )}

        {pile && <Activity events={events} live={live} running={pile.status === "running"} />}
      </main>

      {/* Deliberately NOT wrapped in AnimatePresence. Its exit never completed
        * here — the overlay animated to `opacity: 0` and stayed in the DOM with
        * `pointer-events: auto`, an invisible sheet over the whole app
        * swallowing every click. Adding a `key` did not fix it; the nested
        * spring on the card kept the subtree from ever settling. The third time
        * this component has stalled an exit in this codebase.
        *
        * A dismissal has nothing to animate out of anyway: the useful behaviour
        * is that it is gone the instant it is clicked. */}
      {/* With no pile yet the same guide already fills the workspace inline; a modal
       * over it would say everything twice. */}
      {!welcomed && !(pilesLoaded && piles.length === 0) && (
        <SetupGuide health={health} variant="modal" onDismiss={dismissWelcome} />
      )}

      <AnimatePresence>
        {toast && (
          <motion.div
            className={`toast${toast.bad ? " toast--bad" : ""}`}
            // Announced, not just drawn. A decision result and a load failure
            // are the two things a screen reader user most needs, and a toast
            // that only appears visually tells them neither. `assertive` for a
            // failure because it interrupts what they are doing, which is the
            // point of it.
            role={toast.bad ? "alert" : "status"}
            aria-live={toast.bad ? "assertive" : "polite"}
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 420, damping: 32 }}
          >
            {toast.text}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
