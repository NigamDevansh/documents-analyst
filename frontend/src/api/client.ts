import type {
  Citation,
  Cost,
  DecisionItem,
  DocumentText,
  Finding,
  Health,
  PileSummary,
  Register,
} from "./types";

/** Every call the dashboard makes, in one place. Same origin always — FastAPI
 * serves this bundle in production, Vite proxies the API in development — so
 * there is no base URL and no CORS.
 */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    // The API's `detail` is written to be read by a person — surface it rather
    // than "Request failed", which sends someone to the network tab to find
    // the sentence we already wrote.
    let detail = response.statusText;
    try {
      detail = (await response.json())?.detail ?? detail;
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new ApiError(String(detail), response.status);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => call<Health>("/health"),
  piles: () => call<{ piles: PileSummary[] }>("/piles").then((b) => b.piles),
  register: (pile: string) => call<Register>(`/piles/${pile}/deliverable`),
  citations: (pile: string) =>
    call<{ citations: Citation[] }>(`/piles/${pile}/citations`).then((b) => b.citations),
  queue: (pile: string) =>
    call<{ items: DecisionItem[] }>(`/piles/${pile}/decisions`).then((b) => b.items),
  findings: (pile: string) =>
    call<{ findings: Finding[] }>(`/piles/${pile}/findings`).then((b) => b.findings),
  documentText: (id: string) => call<DocumentText>(`/documents/${id}/text`),
  cost: (pile: string) => call<Cost>(`/piles/${pile}/cost`),

  decide: (id: string, approve: boolean, actor: string, body: { note?: string; chosen_value_group?: number }) =>
    call<{ decision: { status: string; decided_by: string } }>(
      `/decisions/${id}/${approve ? "approve" : "reject"}`,
      { method: "POST", headers: { "X-Actor": actor }, body: JSON.stringify(body) },
    ),
};
