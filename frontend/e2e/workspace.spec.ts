import { test, expect, type Page } from "@playwright/test";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const D1 = "33333333-3333-4333-8333-333333333333";
const D2 = "44444444-4444-4444-8444-444444444444";
const health = { phase: "dashboard", degraded: false, database: { reachable: true }, events: { listening: true }, missing_configuration: [], setup: { ready: true, requirements: [] }, watch: { path: "./watched", scan_interval_s: 30, pile_concurrency: 3 } };
const register = (id: string) => ({ pile_id: id, version: id === A ? 1 : 2,
  document: { filename: "final-document.docx", written: true, download: `/piles/${id}/deliverable/document` },
  slots: [{ slot_id: id, agreement_id: id, agreement_label: "Acme and Beta", field_key: "payment_terms_days", label: `${id === A ? "A" : "B"} term`, state: "settled", values: [], current: { text: "Net 30 days" } }] });
const options = [0, 1].map(n => ({ value_group: n, value_text: n === 0 ? "Net 30" : "Net 45", sources: [{ filename: "source.md" }] }));

async function fixture(page: Page) {
  let ready = true;
  let queue = [{ id: D1, kind: "conflict", ref_id: A, payload: { options }, recommendation: "Choose a payment term" }];
  let submitted: unknown = null;
  const uploads: string[] = [];
  const piles = [A, B].map((id, i) => ({ id, name: i ? "Beta pile" : "Alpha pile", status: "idle", documents: 2, quarantined: 0, pending_decisions: 1, open_conflicts: 1, has_rulebook: true, leased: false }));
  await page.route("**/*", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/health") return route.fulfill({ json: { ...health, setup: { ...health.setup, ready } } });
    if (!path.startsWith("/piles") && !path.startsWith("/documents") && !path.startsWith("/decisions")) return route.continue();
    if (path === "/piles") return route.fulfill({ json: { piles } });
    if (path.endsWith("/events")) return route.fulfill({ contentType: "text/event-stream", body: ": connected\n\n" });
    if (path.endsWith("/deliverable/document")) return route.fulfill({ headers: { "content-disposition": 'attachment; filename="final-document.docx"' }, contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", body: Buffer.from("test document") });
    if (path.endsWith("/deliverable")) return route.fulfill({ json: register(path.includes(A) ? A : B) });
    if (path.endsWith("/citations")) return route.fulfill({ json: { citations: [D1, D2].map((id, i) => ({ id, slot_id: path.includes(A) ? A : B, source_document_id: id, filename: i ? "second.md" : "first.md", source_quote: "Net 30 days", char_start: 2, char_end: 13 })) } });
    if (path.startsWith("/documents/")) return route.fulfill({ json: { id: path.includes(D1) ? D1 : D2, filename: path.includes(D1) ? "first.md" : "second.md", text: "😀 Net 30 days", length: 13 } });
    if (path.endsWith("/decisions")) return route.fulfill({ json: { items: queue } });
    if (path.startsWith("/decisions/")) { submitted = route.request().postDataJSON(); queue = []; return route.fulfill({ json: { decision: { status: "approved", decided_by: "Test reviewer" } } }); }
    if (path.endsWith("/findings")) return route.fulfill({ json: { findings: [] } });
    if (path.endsWith("/cost")) return route.fulfill({ json: { cost_usd: 0, calls: 0, failures: 0, latency_ms: 0, by_node: [] } });
    if (path.endsWith("/documents") && route.request().method() === "POST") { uploads.push(route.request().headers()["content-type"] ?? ""); return route.fulfill({ status: 202, json: { accepted: true } }); }
    return route.fulfill({ status: 404, json: { detail: "Unknown fixture endpoint" } });
  });
  return { setReady: (value: boolean) => { ready = value; }, submitted: () => submitted, uploads: () => uploads };
}

async function open(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Got it", exact: true }).click();
  await expect(page.getByRole("heading", { name: "A term" })).toHaveCount(0); // term labels are not navigation headings
  await expect(page.locator(".term__label")).toHaveText("A term");
}

test("late pile responses cannot replace another pile's document or review queue", async ({ page }) => {
  await fixture(page); await open(page);
  let release!: () => void, started!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route(`**/piles/${A}/deliverable`, async route => { started(); await waiting; await route.fulfill({ json: register(A) }); });
  await page.getByRole("button", { name: /Beta pile/ }).click();
  await expect(page.locator(".term__label")).toHaveText("B term");
  await page.getByRole("button", { name: /Alpha pile/ }).click(); await requested;
  await page.getByRole("button", { name: /Beta pile/ }).click();
  await expect(page.locator(".term__label")).toHaveText("B term"); release();
  await expect(page.locator("a[download]")).toHaveAttribute("href", `/piles/${B}/deliverable/document`);
  await expect(page.locator(".term__label")).toHaveText("B term");
});

test("late source text cannot replace the selected citation; Unicode offsets stay exact", async ({ page }) => {
  await fixture(page); await open(page);
  let release!: () => void, started!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route(`**/documents/${D1}/text`, async route => { started(); await waiting; await route.fulfill({ json: { id: D1, filename: "first.md", text: "wrong source", length: 12 } }); });
  await page.getByRole("button", { name: /first.md/ }).click(); await requested;
  await page.getByRole("button", { name: /second.md/ }).click();
  await expect(page.locator(".source__file")).toHaveText("second.md");
  release();
  await expect(page.locator("mark")).toHaveText("Net 30 days");
  await expect(page.locator(".source__file")).toHaveText("second.md");
});


test("phone layout keeps navigation, document download, and source usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await fixture(page); await open(page);
  await expect(page.locator(".rail")).toBeHidden();
  await page.getByRole("button", { name: "Piles", exact: true }).click();
  await page.getByRole("button", { name: /Beta pile/ }).click(); await expect(page.locator(".rail")).toBeHidden();
  await expect(page.locator(".term__label")).toHaveText("B term");
  const box = await page.locator("a[download]").boundingBox(); expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  await page.getByRole("button", { name: /second.md/ }).click(); await expect(page.locator("mark")).toHaveText("Net 30 days");
});

test("desktop layout keeps the rail beside the workspace and hides the phone menu", async ({ page }) => {
  // `.btn` is declared after `.mobile-nav`, so a same-specificity hide rule loses and the
  // phone button becomes a grid cell, pushing the rail into the workspace column.
  await page.setViewportSize({ width: 1280, height: 800 }); await fixture(page); await open(page);
  await expect(page.getByRole("button", { name: "Piles", exact: true })).toBeHidden();
  const rail = (await page.locator(".rail").boundingBox())!;
  const main = (await page.locator(".main").boundingBox())!;
  expect(rail.x).toBe(0);
  expect(main.x).toBeGreaterThanOrEqual(rail.x + rail.width - 1);
  expect(main.y).toBe(rail.y);
  expect(main.height).toBeGreaterThan(700);
});

test("files can be uploaded into the open pile", async ({ page }) => {
  const app = await fixture(page); await open(page);
  await page.locator('input[type="file"]').setInputFiles([
    { name: "amendment.md", mimeType: "text/markdown", buffer: Buffer.from("Net 45") },
    { name: "letter.md", mimeType: "text/markdown", buffer: Buffer.from("Net 60") },
  ]);
  await expect(page.getByText("Uploaded 2 files")).toBeVisible();
  expect(app.uploads()).toHaveLength(2);
  expect(app.uploads()[0]).toContain("multipart/form-data");
});

test("welcome traps focus and Escape restores the workspace", async ({ page }) => {
  await fixture(page); await page.goto("/");
  const done = page.getByRole("button", { name: "Got it", exact: true }); await expect(done).toBeFocused();
  await page.keyboard.press("Shift+Tab"); await expect(done).toBeFocused();
  await page.keyboard.press("Tab"); await expect(done).toBeFocused();
  await page.keyboard.press("Escape"); await expect(page.locator(".welcome")).toHaveCount(0);
  await expect(page.locator(".term__label")).toHaveText("A term");
});

test("review choices support arrow keys and submit the chosen value", async ({ page }) => {
  const app = await fixture(page); await open(page);
  await page.getByLabel("Your name").fill("Test reviewer"); await page.getByLabel("Your name").press("Enter");
  await page.getByRole("tab", { name: /Review/ }).click();
  const radio = page.getByRole("radio").first(); await radio.focus(); await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("radio").nth(1)).toBeFocused();
  await expect(page.getByRole("radio").nth(1)).toHaveAttribute("aria-checked", "true");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  expect(app.submitted()).toEqual({ chosen_value_group: 1 });
  await expect(page.getByText("Nothing waiting", { exact: true })).toBeVisible();
});

test("setup unlocks after missing credentials become ready", async ({ page }) => {
  const app = await fixture(page); app.setReady(false); await page.goto("/");
  await expect(page.getByRole("heading", { name: "Add your API keys to start" })).toBeVisible();
  app.setReady(true); await page.getByRole("button", { name: "Check again", exact: true }).click();
  await page.getByRole("button", { name: "Got it", exact: true }).click();
  await expect(page.locator(".term__label")).toHaveText("A term");
});
