// All API requests are intercepted. No real recording, computation or upload is created.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.PAGE_BASE_URL || "http://127.0.0.1:9001";
assert.equal(new URL(base).hostname, "127.0.0.1");
const output = resolve(process.env.RECORDING_ARTIFACT_DIR || "/tmp/nexpoly-browsing-recording");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const failures = [];
const results = [];
const requests = [];
let knowledgeFixture = false;
let startFailures = 0;
let summaryResponse;
let summaryId;
let resolveSummary;
let summaryReady = new Promise(resolve => { resolveSummary = resolve; });
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
// A local HTTP fixture emits real chunks; route.fulfill would buffer the whole body.
const summaryServer = createServer((req, res) => {
  req.resume();
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "*");
  if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
  summaryId = req.url.split("/").at(-2);
  summaryResponse = res;
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  res.write(frame("status", { phase: "summarizing" }));
  resolveSummary();
});
await new Promise(resolve => summaryServer.listen(0, "127.0.0.1", resolve));
function completeSummary(summary) {
  summaryResponse.end(frame("done", { recording_id: summaryId, summary, generated: true }));
  summaryResponse = undefined;
  summaryReady = new Promise(resolve => { resolveSummary = resolve; });
}
page.on("pageerror", error => failures.push(error.message));
await context.route("**/*", async route => {
  const req = route.request();
  const url = new URL(req.url());
  if (url.pathname.startsWith("/api/")) {
    if (knowledgeFixture && url.pathname.endsWith("/knowledge/search")) return route.fulfill({ json: {
      query: "polyimide", groups: [{ terms: ["polyimide"] }], terms: ["polyimide"], search_id: "ui-search",
      page: 1, page_size: 20, total: 1, query_time_ms: 2, results: [{ knowledge_id: 1,
        title_zh: "聚合物测试文献", title_en: "Polymer paper", source_file: "fixture", source_row_number: 1,
        abstract: "Polymer abstract", abstract_snippet: "Polymer abstract", matched_terms: ["polyimide"], matched_fields: ["Title"] }]
    } });
    if (knowledgeFixture && url.pathname.endsWith("/knowledge/observations")) return route.fulfill({ json: {
      event: "article.opened", search_id: "ui-search", knowledge_id: 1
    } });
    if (url.pathname.includes("/knowledge/recordings")) {
      requests.push({ path: url.pathname, body: req.postDataJSON() });
      if (url.pathname.endsWith("/recordings") && startFailures > 0) {
        startFailures--;
        return route.fulfill({ status: 404, json: { detail: "Not Found" } });
      }
      if (url.pathname.endsWith("/summary")) {
        assert.equal(req.headers().accept, "text/event-stream");
        return route.continue({ url: `http://127.0.0.1:${summaryServer.address().port}${url.pathname}` });
      }
      const id = url.pathname.endsWith("/stop") ? url.pathname.split("/").at(-2) : req.postDataJSON().recording_id;
      return route.fulfill({ json: { recording_id: id, status: url.pathname.endsWith("/stop") ? "stopped" : "recording", events: [] } });
    }
    return route.fulfill({ status: 503, json: { detail: "Local UI fixture" } });
  }
  if (!["GET", "HEAD"].includes(req.method()) || (url.protocol.startsWith("http") && url.hostname !== "127.0.0.1")) return route.abort();
  return route.continue();
});
const entry = () => page.locator("[data-recording-entry] > button");
async function checkHost(path, width) {
  await entry().waitFor();
  assert.equal(await page.locator("[data-recording-entry]").count(), 1, `${path}: only one mounted entry`);
  const data = await page.evaluate(() => {
    const e = document.querySelector("[data-recording-entry]");
    const b = e.querySelector("button");
    const main = document.querySelector(".np-app-shell__body > main").getBoundingClientRect();
    const rect = b.getBoundingClientRect();
    const header = b.closest(".np-module-page-header, [data-recording-header]");
    const status = header?.querySelector(".ks-toolbar-status, .dbf-tool-status")?.getBoundingClientRect();
    return { host: e.dataset.recordingEntry, disabled: b.disabled, top: main.top,
      h: rect.height, w: rect.width, x: rect.x, y: rect.y,
      inHeader: Boolean(header), headerBottom: header?.getBoundingClientRect().bottom,
      status: status ? { right: status.right, y: status.y, height: status.height } : null,
      overflow: document.documentElement.scrollWidth > innerWidth + 1 };
  });
  const toolbarEntry = ["/knowledge", "/database-filter"].includes(path);
  assert.equal(data.host, width < 1024 ? "mobile" : path === "/" ? "sidebar" : toolbarEntry ? "toolbar" : "page");
  assert.equal(data.top, width < 1024 ? 56 : 0);
  assert.equal(data.overflow, false);
  assert.ok(data.x >= 0 && data.x + data.w <= width + 1, `${path} at ${width}px: entry outside viewport ${JSON.stringify(data)}`);
  if (width < 1024) { assert.equal(data.h, 40); assert.equal(data.w, 40); }
  else if (path !== "/") { assert.ok(data.inHeader); assert.ok(data.y + data.h <= data.headerBottom + 1); }
  if (width >= 1024 && toolbarEntry) {
    assert.ok(data.status, `${path}: ready status beside recording entry`);
    assert.ok(data.x > data.status.right && data.x - data.status.right <= 13);
    assert.ok(Math.abs(data.y - data.status.y) <= 1 && Math.abs(data.h - data.status.height) <= 1);
  }
  results.push({ path, width, ...data });
  return data;
}
async function navigate(path) {
  // SPA history navigation also verifies that a mounted panel survives page replacement.
  await page.evaluate(path => {
    history.pushState({}, "", path);
    dispatchEvent(new PopStateEvent("popstate"));
  }, path);
  const module = { "/": "home", "/knowledge": "knowledge", "/database-filter": "databaseFilter", "/database-query": "databaseQuery" }[path];
  await page.waitForFunction(module => {
    const content = document.querySelector("[data-module-phase]");
    return content?.dataset.modulePhase === "idle" && content.dataset.moduleContent === module;
  }, module);
  await entry().waitFor();
}
async function checkPanel() {
  assert.equal(await page.locator(".ks-recording-panel").count(), 1);
  const rect = await page.locator(".ks-recording-panel").boundingBox();
  const viewport = page.viewportSize();
  assert.ok(rect.x >= 0 && rect.y >= 0 && rect.x + rect.width <= viewport.width + 1);
  assert.ok(rect.y + rect.height <= viewport.height + 1);
}

try {
  // Cold loads include pages that never load knowledge-retrieval.css.
  for (const [width, height] of [[390,844], [1023,768], [1024,768], [1440,900], [2560,1440]]) {
    await page.setViewportSize({ width, height });
    for (const path of ["/database-query", "/database-filter", "/knowledge", "/", "/lab-data/collect", "/lab-data/dashboard", "/experiment-workflow-demo"]) {
      await page.goto(base + path, { waitUntil: "domcontentloaded" });
      // The mobile entry mounts before the lazy page has loaded its host.
      if (!["/"].includes(path)) await page.getByText("页面加载中…", { exact: true }).waitFor({ state: "hidden" });
      const data = await checkHost(path, width);
      assert.equal(data.disabled, !["/knowledge", "/database-filter"].includes(path));
      if (data.disabled) {
        await page.locator("[data-recording-entry]").focus();
        const hint = page.locator(".np-recording-tooltip.is-visible");
        await hint.waitFor();
        const hintRect = await hint.boundingBox();
        assert.ok(hintRect.x >= 0 && hintRect.x + hintRect.width <= width + 1, `${path}: support hint within viewport`);
        await page.locator("[data-recording-entry]").evaluate(e => e.blur());
      }
      if ([390,1440].includes(width) && ["/database-query", "/knowledge", "/"].includes(path)) {
        await page.screenshot({ path: resolve(output, `${path.slice(1) || "home"}-${width}.png`) });
      }
      if (path === "/experiment-workflow-demo") {
        const tabs = page.locator(".top-view-nav button");
        for (let i = 0; i < await tabs.count(); i++) { await tabs.nth(i).click(); await checkHost(path, width); }
      }
    }
  }
  assert.equal(requests.length, 0, "cold loads must not start recording");

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(base + "/database-filter", { waitUntil: "domcontentloaded" });
  startFailures = 2;
  await entry().click();
  const startError = page.locator(".np-recording-tooltip.is-visible");
  await page.getByRole("alert").filter({ hasText: "当前服务尚未启用浏览记录功能" }).waitFor();
  await page.getByRole("heading", { name: "数据库筛选", exact: true }).click();
  assert.equal(await startError.count(), 0, "outside click dismisses a retained start error");
  assert.equal(await page.locator(".np-recording-warning").count(), 1);
  await entry().focus();
  await startError.waitFor();
  await page.keyboard.press("Escape");
  assert.equal(await startError.count(), 0, "Escape dismisses the start error");
  await entry().click();
  await startError.waitFor();
  assert.equal(requests[0].body.recording_id, requests[1].body.recording_id, "failed starts retry the same ID");
  await page.getByRole("heading", { name: "数据库筛选", exact: true }).click();
  assert.equal(await startError.count(), 0);
  await page.screenshot({ path: resolve(output, "start-error-dismissed.png") });
  requests.length = 0;
  await page.goto(base + "/knowledge", { waitUntil: "domcontentloaded" });
  await entry().waitFor();
  for (const tab of ["在线文献", "PDF 相似度"]) {
    await page.getByRole("tab", { name: new RegExp(tab) }).click();
    await checkHost("/knowledge", 1440);
    assert.ok(await entry().isDisabled());
    await page.getByRole("tab", { name: "本地知识库", exact: true }).click();
    assert.equal(await entry().isDisabled(), false);
  }
  await entry().click();
  await page.getByRole("button", { name: "结束并总结", exact: true }).waitFor();
  const recordingId = requests[0].body.recording_id;
  await navigate("/database-filter");
  await checkHost("/database-filter", 1440);
  await navigate("/database-query");
  assert.equal(await entry().isDisabled(), false);
  await navigate("/knowledge");
  await entry().click();
  await page.getByText("正在整理本次阅读", { exact: true }).waitFor();
  await summaryReady;
  summaryResponse.write(frame("delta", { text: "检索线索。" }));
  await page.getByRole("region", { name: "正在生成的总结", exact: true }).getByText("检索线索。", { exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, "streaming-desktop.png") });
  await checkPanel();
  const panelId = await page.locator(".ks-recording-panel").getAttribute("id");
  await navigate("/database-query");
  await checkPanel();
  assert.equal(await page.locator(".ks-recording-panel").getAttribute("id"), panelId);
  assert.ok(await page.locator(".ks-recording-scope").isVisible());
  await page.setViewportSize({ width: 390, height: 844 });
  await checkHost("/database-query", 390);
  await checkPanel();
  await page.screenshot({ path: resolve(output, "summary-mobile.png") });
  await page.keyboard.press("Escape");
  await page.locator(".ks-recording-panel").waitFor({ state: "detached" });
  assert.ok(await entry().evaluate(e => e === document.activeElement));
  summaryResponse.write(frame("delta", { text: "\n\n阅读收获。" }));
  await entry().click();
  await page.getByRole("region", { name: "正在生成的总结", exact: true }).getByText("阅读收获。", { exact: true }).waitFor();
  await checkPanel();
  await page.screenshot({ path: resolve(output, "streaming-mobile.png") });
  await page.keyboard.press("Escape");
  await page.locator(".ks-recording-panel").waitFor({ state: "detached" });
  completeSummary("检索线索。\n\n阅读收获。\n\n内容联系。");
  await page.getByRole("button", { name: "AI 浏览总结：查看总结", exact: true }).waitFor();
  assert.equal(await page.locator(".ks-recording-panel").count(), 0);
  await entry().click();
  assert.equal(await page.getByRole("button", { name: "开始新记录", exact: true }).count(), 0);
  await navigate("/");
  await page.setViewportSize({ width: 1440, height: 900 });
  await checkHost("/", 1440);
  await checkPanel();
  await navigate("/database-filter");
  await page.getByRole("button", { name: "开始新记录", exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, "summary-desktop.png") });
  await page.getByRole("button", { name: "关闭总结", exact: true }).focus();
  await page.getByRole("heading", { name: "数据库筛选", exact: true }).click();
  await page.waitForFunction(() => document.activeElement === document.querySelector("[data-recording-entry] > button"));
  assert.equal(await page.locator(".ks-recording-panel").count(), 0);
  await entry().click();
  assert.equal(requests.length, 3, "one start, stop and summary across routes and viewport changes");
  assert.ok(requests[1].path.includes(recordingId) && requests[2].path.includes(recordingId));
  await page.getByRole("button", { name: "开始新记录", exact: true }).click();
  await page.getByRole("button", { name: "结束并总结", exact: true }).waitFor();
  assert.equal(await page.locator(".ks-recording-panel").count(), 0);
  assert.notEqual(requests[3].body.recording_id, recordingId);

  // A retained article drawer must not make the foreground summary inert on resize.
  knowledgeFixture = true;
  await navigate("/knowledge");
  await page.getByRole("searchbox", { name: "本地知识库检索词" }).fill("polyimide");
  await page.getByRole("button", { name: "运行检索", exact: true }).click();
  await page.getByRole("button", { name: /聚合物测试文献/ }).click();
  await page.locator(".ks-detail-drawer.is-open").waitFor();
  await entry().click();
  await page.getByText("正在整理本次阅读", { exact: true }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await checkHost("/knowledge", 390);
  await checkPanel();
  assert.equal(await page.locator(".ks-recording-panel").evaluate(e => Boolean(e.closest("[inert]"))), false);
  await page.getByRole("button", { name: "关闭总结", exact: true }).focus();
  await page.keyboard.press("Tab");
  assert.ok(await page.locator(".ks-recording-panel").evaluate(e => e.contains(document.activeElement)));
  await page.keyboard.press("Escape");
  await page.locator(".ks-recording-panel").waitFor({ state: "detached" });
  assert.ok(await page.locator(".ks-detail-drawer.is-open").isVisible());
  await page.waitForFunction(() => document.querySelector('.ks-detail-drawer.is-open')?.contains(document.activeElement));
  await page.keyboard.press("Escape");
  await page.locator(".ks-detail-drawer.is-open").waitFor({ state: "hidden" });
  await summaryReady;
  completeSummary("弹层焦点验证。");
  assert.deepEqual(failures, []);
} finally {
  await writeFile(resolve(output, "report.json"), JSON.stringify({ results, failures, requests }, null, 2));
  await browser.close();
  summaryResponse?.destroy();
  summaryServer.closeAllConnections();
  await new Promise(resolve => summaryServer.close(resolve));
}
console.log(`Passed ${results.length} host checks and cross-module recording lifecycle checks. Artifacts: ${output}`);
