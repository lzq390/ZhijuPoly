// Run against the dev frontend only. All mutations are fulfilled with fixtures;
// external iframe/API requests are blocked. No jobs or user records are created.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const { chromium } = await import(process.env.MOTION_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MOTION_BASE_URL || "http://127.0.0.1:9001";
assert.equal(new URL(base).hostname, "127.0.0.1", "This check is restricted to the local dev frontend");
const output = resolve(process.env.MOTION_ARTIFACT_DIR || "/tmp/nexpoly-motion-browser");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.MOTION_CHROMIUM_PATH || undefined, headless: true, args: ["--no-sandbox"] });
const results = [];
const reducedControl = process.env.MOTION_PROFILE_REDUCED === "true";

function knowledgeFixture(payload) {
  return { query: payload.query, groups: [{ terms: [payload.query] }], terms: [payload.query], page: 1, page_size: 20, query_time_ms: 12, total: 1,
    results: [{ knowledge_id: 1, title_zh: "视觉验证：聚酰亚胺合成记录", title_en: "Motion verification fixture",
      abstract: "Browser verification fixture; no real records are changed.", abstract_snippet: "聚合物合成条件与溯源信息。", claim: "Fixture claim", analysis: "Fixture analysis",
      source_file: "motion-fixture.jsonl", source_row_number: 1, source_sequence: "MOTION-1", is_polymer_synthesis: "yes", judgement_reason: "Fixture",
      polymer_iupac: "polyimide", formulation: "dianhydride + diamine", catalyst: "", temperature: "80 °C", reaction_time: "4 h", solvent: "NMP", matched_terms: [payload.query], matched_fields: ["Polymer"] }] };
}
async function traceStart(page) {
  const client = await page.context().newCDPSession(page);
  await client.send("Tracing.start", { categories: "devtools.timeline,blink.user_timing,toplevel", transferMode: "ReturnAsStream" });
  return async (name) => {
    const completed = new Promise((done) => client.once("Tracing.tracingComplete", done));
    await client.send("Tracing.end");
    const { stream } = await completed;
    let data = "";
    while (true) {
      const part = await client.send("IO.read", { handle: stream });
      data += part.base64Encoded ? Buffer.from(part.data, "base64").toString() : part.data;
      if (part.eof) break;
    }
    await client.send("IO.close", { handle: stream });
    await writeFile(resolve(output, `${name}.trace.json`), data);
    const events = JSON.parse(data).traceEvents;
    await client.detach();
    return { layoutEvents: events.filter((event) => event.name === "Layout" && event.ph === "X").length,
      layoutMs: Math.round(events.filter((event) => event.name === "Layout" && event.ph === "X").reduce((sum, event) => sum + (event.dur || 0), 0) / 1000) };
  };
}
async function settle(page, selector) {
  await page.waitForFunction((selector) => document.querySelector(selector)?.getAttribute("data-motion-phase") === "open", selector);
}
async function phase(page, name) { await page.evaluate((name) => { window.__motionPhase = name; performance.mark(name); }, name); }
async function navigation(page, width, id) {
  if (width < 1024) await page.getByRole("button", { name: "打开导航", exact: true }).click();
  const root = page.locator(width < 1024 ? "#np-mobile-navigation" : ".np-sidebar-desktop");
  const button = root.locator(`[data-module-id="${id}"]`);
  if (!(await button.count())) {
    for (const trigger of await root.locator('.np-sidebar-group__trigger[aria-expanded="false"]:not(:disabled)').all()) await trigger.click();
  }
  await button.click();
  await page.locator(`[data-module-content="${id}"][data-module-phase="idle"]:not([inert])`).waitFor();
  if (width < 1024) await page.locator(".np-sidebar-mobile-layer").waitFor({ state: "detached" });
}
async function checkDrag(page, selector) {
  const handle = page.locator(`${selector} [role="separator"]`);
  const box = await handle.boundingBox();
  assert.ok(box);
  const before = await page.locator(selector).boundingBox();
  await phase(page, "resize");
  // The outside half of the handle may be clipped by the drawer's border.
  const start = box.x + box.width - 2;
  await page.mouse.move(start, box.y + box.height / 2);
  await page.mouse.down();
  for (let i = 1; i <= 12; i++) await page.mouse.move(start - i * 6, box.y + box.height / 2);
  await page.mouse.up();
  // Observe the committed pointerup state, not React's pending event batch.
  await page.waitForFunction((selector) => !document.querySelector(selector)?.closest(".is-resizing"), selector);
  const first = await page.locator(selector).boundingBox();
  await page.waitForTimeout(240);
  const final = await page.locator(selector).boundingBox();
  assert.ok(final.width > before.width + 20, "Dragging changes the actual drawer width");
  assert.ok(Math.abs(first.width - final.width) < 1, "No trailing width transition after pointerup");
  await phase(page, "after-resize");
  return Math.round(final.width);
}

try {
  const viewports = [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 390, height: 844 }, { width: 2560, height: 1440 }]
    .filter((viewport) => !process.env.MOTION_VIEWPORT || process.env.MOTION_VIEWPORT === String(viewport.width));
  for (const viewport of viewports) {
    const name = `${viewport.width}x${viewport.height}`;
    const context = await browser.newContext({ viewport, reducedMotion: reducedControl ? "reduce" : "no-preference" });
    await context.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== new URL(base).origin) return route.abort();
      if (url.pathname.endsWith("/knowledge/search")) return route.fulfill({ json: knowledgeFixture(request.postDataJSON()) });
      if (url.pathname.endsWith("/monomer-retrosynthesis")) return route.fulfill({ json: { total: 0, candidates: [], input_smiles: "CCO", target_role: "auto" } });
      if (!["GET", "HEAD"].includes(request.method())) return route.abort();
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    await page.addInitScript(() => {
      window.__motionPhase = "load";
      window.__motionLongTasks = [];
      window.__motionFrames = [];
      let previous;
      function frame(time) {
        if (previous && window.__motionPhase !== "load") window.__motionFrames.push({ phase: window.__motionPhase, duration: time - previous });
        previous = time;
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) window.__motionLongTasks.push({ phase: window.__motionPhase, start: Math.round(entry.startTime), duration: Math.round(entry.duration) });
      }).observe({ type: "longtask", buffered: true });
    });
    const result = { viewport: name, reducedControl };
    let finishTrace;
    try {
      await page.goto(`${base}/structure-workbench`);
      await page.locator(".np-sw-editor iframe").waitFor();
      await page.waitForFunction(() => !!document.querySelector(".np-sw-editor iframe")?.contentWindow?.ketcher, null, { timeout: 30000 });
      await page.waitForTimeout(350);
      finishTrace = await traceStart(page);
      await phase(page, "rapid-navigation");
      if (viewport.width < 1024) await page.getByRole("button", { name: "打开导航", exact: true }).click();
      const nav = page.locator(viewport.width < 1024 ? "#np-mobile-navigation" : ".np-sidebar-desktop");
      await nav.locator('[data-group-id="discover"] .np-sidebar-group__trigger').click();
      await nav.evaluate((root) => {
        window.__editorBefore = document.querySelector(".np-sw-editor iframe");
        for (const id of ["knowledge", "databaseQuery", "structureWorkbench"]) root.querySelector(`[data-module-id="${id}"]`).click();
      });
      await page.waitForTimeout(1650);
      result.retainedIframe = await page.evaluate(() => document.querySelector(".np-sw-editor iframe") === window.__editorBefore);
      assert.equal(result.retainedIframe, true);
      assert.equal(await page.locator("[data-module-content]").getAttribute("data-module-content"), "structureWorkbench");

      await phase(page, "workbench-drawer");
      await page.getByRole("button", { name: "功能参数", exact: true }).click();
      await page.getByRole("button", { name: "设置单体逆合成反推参数" }).click();
      await page.getByRole("textbox", { name: "目标单体 SMILES" }).fill("CCO");
      await page.getByRole("button", { name: "运行反推", exact: true }).click();
      await settle(page, ".np-sw-drawer");
      const inline = !(await page.locator(".np-sw-drawer-layer").getAttribute("class")).includes("is-overlay");
      result.workbenchMode = inline ? "inline" : "overlay";
      if (viewport.width >= 1024) assert.equal(await page.locator(".np-sidebar-desktop").evaluate((element) => element.inert), false, "Workbench result overlay must not block platform navigation");
      if (inline) result.workbenchResizedWidth = await checkDrag(page, ".np-sw-drawer");
      const layoutBefore = await page.locator(".np-sw-layout").boundingBox();
      await page.getByRole("button", { name: "关闭单体反推结果", exact: true }).click();
      const layoutDuring = await page.locator(".np-sw-layout").boundingBox();
      if (!reducedControl) {
        if (inline) assert.ok(Math.abs(layoutBefore.width - layoutDuring.width) < 1, "Inline slot retained during exit");
        assert.equal(await page.locator(".np-sw-drawer-layer").getAttribute("data-motion-present"), "true");
      }
      await page.getByRole("button", { name: "展开反推结果", exact: true }).waitFor();

      await phase(page, "module-navigation");
      await navigation(page, viewport.width, "knowledge");
      await page.locator(".ks-search-surface input").first().fill("polyimide");
      await page.locator('.ks-search-surface button[type="submit"]').click();
      if (viewport.width < 900) await page.locator(".ks-result-card").first().click();
      await settle(page, ".ks-detail-drawer");
      await phase(page, "knowledge-drawer");
      if (viewport.width >= 900) result.knowledgeResizedWidth = await checkDrag(page, ".ks-detail-drawer");
      const padding = await page.locator(".ks-panel-scroll").evaluate((element) => getComputedStyle(element).paddingRight);
      await page.getByRole("button", { name: "关闭详情", exact: true }).click();
      if (!reducedControl) assert.equal(await page.locator(".ks-panel-scroll").evaluate((element) => getComputedStyle(element).paddingRight), padding);
      await page.locator(".ks-drawer-reopen").waitFor();
      await page.locator(".ks-drawer-reopen").click();
      await settle(page, ".ks-detail-drawer");
      await page.screenshot({ path: resolve(output, `${name}-knowledge.png`) });
      await page.getByRole("button", { name: "关闭详情", exact: true }).click();
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.waitForFunction(() => document.querySelector(".ks-detail-drawer")?.getAttribute("data-motion-phase") === "closed");
      result.runtimeReducedMotion = true;
      await page.emulateMedia({ reducedMotion: reducedControl ? "reduce" : "no-preference" });

      if (viewport.width < 1024) {
        await phase(page, "mobile-navigation");
        await page.getByRole("button", { name: "打开导航", exact: true }).click();
        await settle(page, "#np-mobile-navigation");
        assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "关闭导航");
        assert.equal(await page.locator(".np-app-shell__body").evaluate((element) => element.inert), true);
        await page.keyboard.press("Escape");
        if (!reducedControl) assert.equal(await page.locator(".np-app-shell__body").evaluate((element) => element.inert), true);
        await page.locator(".np-sidebar-mobile-layer").waitFor({ state: "detached" });
        assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "打开导航");
        result.mobileFocus = true;
      }
      result.longTasks = await page.evaluate(() => window.__motionLongTasks.filter((entry) => entry.phase !== "load"));
      result.frames = await page.evaluate(() => {
        const values = window.__motionFrames.map((frame) => frame.duration).sort((a, b) => a - b);
        return { count: values.length, p95Ms: Math.round(values[Math.floor(values.length * 0.95)]), over50Ms: values.filter((duration) => duration > 50).length };
      });
      result.trace = await finishTrace(name);
      finishTrace = null;
      await page.goto(`${base}/high-throughput-workflow-demo`);
      await page.locator(".high-throughput-demo").waitFor();
      result.highThroughput = await page.evaluate(() => ({
        documentOverflow: document.documentElement.scrollHeight > innerHeight || document.documentElement.scrollWidth > innerWidth,
        mainOverflow: getComputedStyle(document.querySelector(".np-app-shell__body > main")).overflowY,
        internalOverflow: getComputedStyle(document.querySelector(".ht-scroll-region")).overflowY,
        height: Math.round(document.querySelector(".high-throughput-demo").getBoundingClientRect().height)
      }));
      assert.equal(result.highThroughput.documentOverflow, false);
      assert.equal(result.highThroughput.mainOverflow, "hidden");
      assert.equal(result.highThroughput.internalOverflow, "auto");
      await page.screenshot({ path: resolve(output, `${name}-high-throughput.png`) });
      result.passed = true;
    } catch (error) {
      result.passed = false;
      result.error = String(error);
      await page.screenshot({ path: resolve(output, `${name}-failure.png`) }).catch(() => {});
      if (finishTrace) result.trace = await finishTrace(`${name}-failure`).catch(() => null);
    }
    results.push(result);
    console.log(JSON.stringify(result));
    await context.close();
  }
} finally {
  await browser.close();
  await writeFile(resolve(output, "results.json"), JSON.stringify(results, null, 2));
}
if (results.some((result) => !result.passed)) process.exitCode = 1;
