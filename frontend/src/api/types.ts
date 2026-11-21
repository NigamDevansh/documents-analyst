/** The shapes the API actually returns. Hand-written, not generated: the routes
 * return plain dicts on purpose, so there is nothing authoritative to generate
 * from. Only the fields this UI uses — anything the API adds later is ignored.
 */

export type PileStatus = "idle" | "running" | "awaiting_review" | "parked" | "error";

export interface PileSummary {
  id: string;
  name: string;
  status: PileStatus;
  documents: number;
  quarantined: number;
  pending_decisions: number;
  open_conflicts: number;
  open_findings: number;
  deliverable_version: number;
  leased: boolean;
  /** A `rulebook.yaml` in the folder. Optional by design — a pile without one
   * still produces a register — but its absence has to be visible, or "no
   * compliance findings" reads as "nothing to report". */
  has_rulebook: boolean;
}

export interface SlotValue {
  value_group: number;
  text: string;
  normalized: string | null;
  document_ids: string[];
  citation_count: number;
}

export interface Slot {
  slot_id: string;
  agreement_id: string;
  /** Which contract the term belongs to, by parties. A pile is a folder of
   * contracts, so the register routinely holds several and a flat list makes
   * three agreements' payment terms look like one term contradicting itself. */
  agreement_label: string;
  field_key: string | null;
  subject_free: string | null;
  label: string;
  state: "settled" | "disputed" | "resolved" | "contested";
  /** Who decided, for a row a person answered rather than a document stated.
   * Null for every other state. */
  resolved_by: string | null;
  /** Every source file behind this row has been deleted from the pile folder.
   * Independent of `state`, which is about whether the sources agreed. The
   * value and its citations still work — the extracted text is ours and outlives
   * the file — which is exactly why this has to be shown rather than inferred. */
  orphaned: boolean;
  current: SlotValue | null;
  values: SlotValue[];
}

/** The generated `final-document.docx`, written into the pile's own folder by the
 * commit that follows review. `written` is false until the first commit. */
export interface DeliverableDocument {
  filename: string;
  written: boolean;
  sha256: string | null;
  download: string | null;
}

export interface Register {
  pile_id: string;
  version: number;
  document: DeliverableDocument;
  slots: Slot[];
}

export interface Citation {
  id: string;
  slot_id: string | null;
  field_key: string | null;
  subject_free: string | null;
  value: { text?: string } | null;
  source_document_id: string;
  filename: string;
  char_start: number;
  char_end: number;
  source_quote: string;
}

export interface DecisionOption {
  value_group: number;
  value_text: string;
  value_norm: string | null;
  sources: { filename: string; quote: string }[];
}

export interface DecisionItem {
  id: string;
  kind: string;
  ref_id: string | null;
  relationship: string | null;
  detected_by: string | null;
  evidence_quote: string | null;
  recommendation: string | null;
  group_key: string | null;
  payload: { field_key?: string; recommended_value_group?: number | null; options?: DecisionOption[] };
}

export interface Finding {
  id: string;
  rule_id: string;
  severity: string;
  /** `check` a rulebook rule · `judge` the LLM tier · `scan` the document
   * itself carrying text aimed at this system. */
  evaluated_by: string;
  message: string;
  status: string;
  citations: { filename?: string; quote?: string; char_start?: number; char_end?: number }[];
}

export interface DocumentText {
  id: string;
  filename: string;
  status: string;
  text: string;
  length: number;
}

export interface Cost {
  cost_usd: string | number;
  calls: number;
  failures: number;
  /** Wall time inside models, retries included. Written per call since the
   * beginning and summed by nothing until it was needed to answer "which stage
   * is slow". */
  latency_ms: number;
  by_node: {
    node: string;
    calls: number;
    tokens_in: number;
    tokens_out: number;
    cost_usd: string | number;
    latency_ms: number;
  }[];
}

export interface StageEvent {
  id: number;
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Health {
  phase: string;
  degraded: boolean;
  database: { reachable: boolean };
  events: { listening: boolean };
  missing_configuration: string[];
  /** Where pile folders go, as the person sees it (the host side of the Docker
   * mount), and how they are processed. Optional: an older API omits it. */
  /** What this deployment needs, as requirements any one key can meet — names and
   * set/missing only, never a value. */
  setup?: {
    requirements: KeyRequirement[];
    ready: boolean;
  };
  watch?: {
    path: string;
    deliverable_filename: string;
    pile_concurrency: number;
    scan_interval_s: number;
  };
}

export interface KeyRequirement {
  id: "llm" | "embeddings";
  title: string;
  purpose: string;
  any_of: { provider: string; label: string; name: string; set: boolean }[];
  met: boolean;
  /** The provider in use (or that will be, once its key is added). */
  using: string;
  /** "auto" = picked from the keys present; otherwise the variable that chose it. */
  chosen_by: string;
  models: string[];
}
