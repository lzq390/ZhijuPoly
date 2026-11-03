// Real browser, synthetic API. Uses a private preview by default; an explicit
// 9001 override may read only the development frontend's static assets. Every
// API request is fulfilled or blocked locally: no live account/AI/GPU requests.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
const { chromium } = await import(process.env.PAGE_PLAYWRIGHT_MODULE || "playwright");
const previewPort = Number(process.env.PAGE_PREVIEW_PORT || 5297);
assert.ok(Number.isInteger(previewPort) && previewPort > 0 && previewPort < 65536, "valid private preview port");
const existingBase = process.env.PAGE_EXISTING_BASE_URL;
assert.ok(!existingBase || ["http://127.0.0.1:9001", "http://localhost:9001"].includes(existingBase), "existing frontend checks are restricted to development port 9001");
const base = existingBase || `http://127.0.0.1:${previewPort}`;
const expectedWorkspaceOrigin = process.env.PAGE_EXPECTED_WORKSPACE_ORIGIN || "https://workspace.ci.invalid";
assert.ok(/^https?:$/.test(new URL(expectedWorkspaceOrigin).protocol) && new URL(expectedWorkspaceOrigin).origin === expectedWorkspaceOrigin,
  "expected workspace is an explicit origin; its requests remain blocked");
const output = resolve(process.env.PAGE_ARTIFACT_DIR || "/tmp/nexpoly-personal-usage");
await mkdir(output, { recursive: true });
const server = existingBase ? null : spawn("npm", ["run", "preview", "--", "--host", "127.0.0.1", "--port", String(previewPort), "--strictPort"], {
  detached: true, stdio: ["ignore", "ignore", "pipe"], env: { ...process.env, DEV_PROXY_TARGET: "http://127.0.0.1:9" }
});
let serverError = ""; server?.stderr.on("data", data => { serverError = (serverError + data).slice(-2000); });
const report = { screenshots: [], fixture_internal_modules: 21, fixture_external_modules: 1, fixture_metrics: 9, static_source: base, scope: "real UI with synthetic API fixtures; all real API requests blocked; not server ownership acceptance", results: [], progress: [], blocked: [], unexpected: [], failures: [] };
let browser, lastPage;
const catalog = JSON.parse(await readFile(new URL("./fixtures/usage-catalog.json", import.meta.url), "utf8"));
const allowances = JSON.parse(await readFile(new URL("./fixtures/entitlements.json", import.meta.url), "utf8"));
const fixedNow = "2026-09-30T16:30:00.000Z"; // October 1 in Beijing, September 30 in UTC and the Americas.
const browserTimezones = ["UTC", "America/Los_Angeles", "Asia/Shanghai", "Pacific/Honolulu", "Europe/London", "Asia/Tokyo", "America/New_York", "Pacific/Auckland"];
// Keep entitlement boundaries supplied by the server. Only timestamps in the
// browser fixture change, so all three record views exercise Beijing midnight.
allowances.operations.items[0].created_at = "2026-10-07T15:59:59.000Z";
allowances.operations.items[0].updated_at = "2026-10-07T16:00:01.000Z";
allowances.operations.items[1].created_at = "2026-10-07T16:00:00.000Z";
allowances.result.operation = { ...allowances.result.operation, ...allowances.operations.items[0] };
const viewports = [
  { width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 1024, height: 768 },
  { width: 1920, height: 1080 }, { width: 2560, height: 1440 }, { width: 2000, height: 1120 },
  { width: 1999, height: 1120 }, { width: 2560, height: 1119 }
];
function session(owner, plan = "member") { return { authenticated: !!owner, account_type: plan, user: owner ? { id: `fixture-owner-${owner}`, username: `测试账号${owner}`, must_change_password: false } : null,
  session_id: owner ? `fixture-session-${owner}` : null, csrf_token: owner ? "fixture-only-csrf" : null, capabilities: { "openscience.use": plan === "member" } }; }
function calendarDate(timestamp, timezone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(timestamp)).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
function resourceItems(owner, period) {
  // Two instants one second apart belong to different Beijing dates and the
  // same UTC date. Derive them from the applied range to keep fixtures in range.
  const midnight = Date.parse(period.end) - 86400000;
  return Array.from({ length: owner === "b" ? 7 : 26 }, (_, i) => ({ module: "monomer_md", task_id: `fixture-${owner}-${i}`, attempt_id: `fixture-attempt-${i}`, kind: "task", status: i === 0 ? "failed" : "completed", submitted_at: new Date(midnight - (i === 1 ? 0 : 1000)).toISOString(), started_at: null, finished_at: null, parent_task_id: null, root_task_id: `fixture-${owner}-${i}`, source: "fixture", metrics: { execution_ms: 0, provider_total_tokens: null, artifact_bytes: null } }));
}
function summary(owner, empty = false, period = {}) {
  const tasks = empty ? 0 : owner === "b" ? 7 : 26;
  const states = empty ? {} : { completed: tasks - 1, failed: 1 };
  const metrics = Object.fromEntries(catalog.metrics.map(metric => [metric.id, { value: null, known_count: 0, unknown_count: tasks }]));
  metrics.execution_ms = { value: empty ? null : 0, known_count: empty ? 0 : tasks - 1, unknown_count: empty ? 0 : 1 };
  metrics.observed_wall_ms = { value: empty ? null : 1200, known_count: empty ? 0 : 1, unknown_count: empty ? 0 : tasks - 1 };
  const timezone = period.timezone || "UTC";
  const buckets = new Map();
  for (const item of empty ? [] : resourceItems(owner, period)) {
    const date = calendarDate(item.submitted_at, timezone);
    const bucket = buckets.get(date) || { date, tasks: 0, states: {} };
    bucket.tasks++; bucket.states[item.status] = (bucket.states[item.status] || 0) + 1;
    buckets.set(date, bucket);
  }
  return { period: { start: period.start, end: period.end, timezone, basis: "task_submitted_at" },
    totals: { tasks, attempts: tasks + (empty ? 0 : 1), child_calls: empty ? 0 : 1, states, metrics },
    modules: empty ? [] : [{ module: "monomer_md", label: "分子动力学", tasks, attempts: tasks + 1, child_calls: 1, states, metrics }],
    daily: [...buckets.values()].sort((a, b) => a.date.localeCompare(b.date)), coverage: { uncovered: ["openscience"], warnings: ["部分历史任务没有计量记录"] } };
}
function activityItems(owner, updated = false) {
  const rows = Array.from({ length: owner === "b" ? 7 : 26 }, (_, i) => ({
    module: i === 1 ? "assistant" : "monomer_md", task_id: `fixture-activity-${owner}-${i}`,
    attempt_id: `fixture-activity-${owner}-${i}-attempt-${i === 0 ? 2 : 1}`, attempt_count: i === 0 ? 2 : 1,
    kind: "task", status: ["queued", "running", "completed", "failed"][i % 4],
    submitted_at: i === 0 ? "2026-09-30T15:59:59.000Z" : i === 1 ? "2026-09-30T16:00:00.000Z" : "2026-09-30T01:02:03.000Z", started_at: i === 1 ? "2026-09-30T16:00:01.000Z" : i % 4 ? "2026-09-30T01:02:04.000Z" : null,
    finished_at: i % 4 >= 2 ? "2026-09-30T01:03:00.000Z" : null,
    updated_at: i < 2 ? "2026-09-30T16:00:01.000Z" : "2026-09-30T01:03:01.000Z", parent_task_id: null,
    root_task_id: `fixture-activity-${owner}-${i}`, source: "fixture",
    metrics: { execution_ms: i % 4 >= 2 ? 0 : null, provider_total_tokens: null, artifact_bytes: null }
  }));
  if (updated) {
    rows[1] = { ...rows[1], status: "completed", finished_at: "2026-09-30T16:10:00.000Z", updated_at: "2026-09-30T16:10:00.000Z" };
    rows.unshift({ ...rows[2], task_id: `fixture-activity-${owner}-new`, root_task_id: `fixture-activity-${owner}-new`, attempt_id: `fixture-activity-${owner}-new-attempt-1`, status: "running", submitted_at: "2026-09-30T16:11:00.000Z", started_at: "2026-09-30T16:11:01.000Z", finished_at: null, updated_at: "2026-09-30T16:11:01.000Z" });
  }
  return rows;
}
async function installFixture(context) {
  const state = { owner: "a", plan: "member", allowanceDisabled: false, paginatedOperations: false, operationUpdates: false, activityUpdates: false, activityMode: "normal", mode: "normal", requests: [], hold: new Set(), releases: new Map() };
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== base) {
      const expected = [expectedWorkspaceOrigin, "https://fonts.googleapis.com", "https://fonts.gstatic.com"].includes(url.origin);
      report.blocked.push({ origin: url.origin, category: expected ? "excluded_external_workspace_or_font" : "unexpected_origin" });
      if (!expected) report.unexpected.push(url.origin);
      return route.abort();
    }
    if (!url.pathname.startsWith("/api/")) {
      if (!["GET", "HEAD"].includes(request.method())) { report.unexpected.push(request.method() + " static"); return route.abort(); }
      return route.continue();
    }
    const json = async (body, status = 200) => {
      const key = url.pathname.split("/").at(-1);
      if (state.hold.delete(key)) await new Promise(resolve => state.releases.set(key, resolve));
      try { return await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body), headers: { "Cache-Control": "no-store" } }); }
      catch { /* privateFetch may abort a retired account/filter/detail request. */ }
    };
    if (url.pathname === "/api/v1/auth/session" && request.method() === "GET") return json(session(state.owner, state.plan));
    const receipt = /^\/api\/v1\/usage\/operations\/([0-9a-f-]+)(\/result)?$/.exec(url.pathname);
    if (request.method() !== "GET" || !receipt && !["/api/v1/usage/catalog", "/api/v1/usage/summary", "/api/v1/usage/records", "/api/v1/usage/entitlements", "/api/v1/usage/operations", "/api/v1/usage/activity"].includes(url.pathname)) {
      report.unexpected.push(request.method() + " " + url.pathname); return json({ detail: "Blocked unexpected fixture API" }, 503);
    }
    if (!state.owner || request.headers()["x-session-context"] !== `fixture-session-${state.owner}` || url.searchParams.has("owner_user_id")) {
      report.unexpected.push(`Invalid private usage session: ${url.pathname}; owner=${state.owner}; session=${request.headers()["x-session-context"]}`);
      return json({ detail: "Fixture session isolation violation" }, 403);
    }
    state.requests.push({ path: url.pathname, query: Object.fromEntries(url.searchParams) });
    if (url.pathname.endsWith("/activity")) {
      if (state.activityMode === "disabled") return json({ code: "usage_metering_disabled", detail: "disabled" }, 503);
      if (state.activityMode === "error") return json({ detail: "任务记录暂时无法读取" }, 500);
      const all = state.activityMode === "empty" ? [] : activityItems(state.owner, state.activityUpdates);
      const matching = url.searchParams.has("module") ? all.filter(item => item.module === url.searchParams.get("module")) : all;
      const offset = Number(url.searchParams.get("offset") || 0);
      return json({ mode: "activity_only", items: matching.slice(offset, offset + 25), offset, limit: 25,
        has_more: offset + 25 < matching.length, modules: catalog.modules.filter(item => item.coverage !== "external").map(({ id, label }) => ({ id, label })),
        warnings: ["任务记录不代表成功次数，完成状态不自动扣减额度。"] });
    }
    if (url.pathname.endsWith("/entitlements")) return json(state.allowanceDisabled ? { enabled: false, mode: "not_enabled", pools: [], policy: null } : { ...allowances.entitlements, tier: state.plan });
    if (url.pathname.endsWith("/operations")) {
      const current = state.operationUpdates ? [
        allowances.operations.items[0], { ...allowances.operations.items[1], state: "succeeded", outcome: "service_completed", qualified: true },
        { ...allowances.operations.items[0], operation_id: "00000000-0000-4000-8000-000000000003", module: "assistant", state: "running", outcome: null }
      ] : allowances.operations.items;
      const all = state.owner !== "a" ? [] : state.paginatedOperations ? Array.from({ length: 26 }, (_, i) => ({ ...allowances.operations.items[i % 2], operation_id: `00000000-0000-4000-8000-${String(i + 1).padStart(12, "0")}` })) : current;
      const matching = url.searchParams.has("module") ? all.filter(item => item.module === url.searchParams.get("module")) : all;
      const offset = Number(url.searchParams.get("offset") || 0);
      return json({ items: matching.slice(offset, offset + 25), offset, limit: 25, has_more: offset + 25 < matching.length });
    }
    if (receipt) {
      const item = state.owner === "a" && allowances.operations.items.find(item => item.operation_id === receipt[1]);
      if (!item) return json({ detail: { code: "usage_operation_not_found" } }, 404);
      if (!receipt[2]) return json(item);
      if (item.state === "unknown") return json({ detail: { code: "usage_outcome_unknown", operation_id: item.operation_id } }, 409);
      return json(allowances.result);
    }
    if (url.pathname.endsWith("/catalog")) return json(catalog);
    if (url.pathname.endsWith("/summary")) {
      if (state.mode === "disabled") return json({ code: "usage_metering_disabled", detail: "disabled" }, 503);
      if (state.mode === "error") return json({ detail: "统计汇总暂时无法读取" }, 500);
      return json(summary(state.owner, state.mode === "empty", Object.fromEntries(url.searchParams)));
    }
    const page = Number(url.searchParams.get("page"));
    const all = state.mode === "empty" ? [] : resourceItems(state.owner, Object.fromEntries(url.searchParams));
    const matching = url.searchParams.has("status") ? all.filter(item => item.status === url.searchParams.get("status")) : all;
    return json({ items: matching.slice((page - 1) * 25, page * 25), page, page_size: 25, total: matching.length });
  });
  return state;
}
async function selectOption(page, label, option) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}
async function tab(page, label) {
  await page.getByRole("tab", { name: label, exact: true }).click();
  assert.equal(await page.getByRole("tab", { name: label, exact: true }).getAttribute("aria-selected"), "true");
  assert.equal(await page.getByRole("tabpanel").count(), 1, "only the selected panel is accessible");
}
async function recordSource(page, label) {
  const button = page.getByRole("button", { name: label, exact: true });
  await button.click();
  assert.equal(await button.getAttribute("aria-pressed"), "true", "selected record source is announced");
}
async function verifyActivity(page, state, viewport, deep) {
  const source = page.getByRole("button", { name: "任务记录", exact: true });
  assert.equal(await source.getAttribute("aria-pressed"), "true", "operation tab defaults to independent task records");
  const table = page.getByRole("region", { name: "任务记录表，可横向滚动", exact: true });
  const rows = table.locator("tbody tr");
  await rows.nth(24).waitFor();
  assert.equal(await rows.count(), 25, "task records use 25-row pages");
  assert.equal(await table.getByRole("columnheader", { name: "提交时间（北京时间）", exact: true }).count(), 1);
  assert.equal(await rows.nth(0).locator("time").innerText(), "2026-09-30 23:59:59", "task before Beijing midnight retains its Beijing date");
  assert.equal(await rows.nth(1).locator("time").innerText(), "2026-10-01 00:00:00", "task at Beijing midnight advances the displayed date");
  for (const [index, label] of ["排队中", "执行中", "已完成", "失败"].entries()) {
    assert.equal(await rows.nth(index).getByText(label, { exact: true }).count(), 1, `task state ${label} is shown`);
  }
  assert.equal(await rows.filter({ has: page.locator('[title="fixture-activity-a-0"]') }).count(), 1, "a task with two attempts appears once");
  assert.equal(await rows.first().getByText("2 次任务尝试", { exact: true }).count(), 1, "the merged task reports its attempt count");
  await capture(page, viewport, "activity");
  const detailRequests = state.requests.length;
  const opener = page.getByRole("button", { name: "查看详情", exact: true }).first();
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "任务记录详情", exact: true });
  await dialog.locator(".np-usage-drawer-id code").filter({ hasText: /^fixture-activity-a-0$/ }).waitFor();
  await dialog.getByText("fixture-activity-a-0-attempt-2", { exact: true }).waitFor();
  assert.equal(await dialog.getByText("2026-09-30 23:59:59", { exact: true }).count(), 1);
  assert.equal(await dialog.getByText("2026-10-01 00:00:01", { exact: true }).count(), 1, "task detail update time crosses the Beijing date boundary");
  assert.equal(await dialog.getByText("2 次（重试合并展示）", { exact: true }).count(), 1);
  assert.equal(await dialog.getByText("fixture-activity-a-0-attempt-1", { exact: true }).count(), 0, "detail uses only the latest attempt");
  assert.ok(await dialog.getByText("未记录", { exact: true }).count(), "unknown task measurements remain unknown");
  const box = await dialog.boundingBox();
  const expectedWidth = viewport.width < 560 ? viewport.width : viewport.width >= 2000 && viewport.height >= 1120 ? 540 : 380;
  assert.ok(Math.abs(box.width - expectedWidth) <= 1, "task detail uses standard drawer widths");
  await capture(page, viewport, "activity-detail");
  if (deep) {
    await dialog.getByRole("button", { name: "复制任务 ID", exact: true }).click();
    await dialog.getByText("任务 ID 已复制", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "fixture-activity-a-0", "copy returns the full task ID");
  }
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "hidden" });
  await page.waitForFunction(() => document.activeElement?.textContent === "查看详情");
  assert.equal(state.requests.length, detailRequests, "viewing or copying task detail performs no additional API or result request");
  await verifyOperationMenu(page, viewport, "-activity", "activity");
  if (!deep) return;

  state.activityUpdates = true;
  await page.getByRole("button", { name: "刷新全部", exact: true }).click();
  await rows.locator('[title="fixture-activity-a-new"]').waitFor();
  const completed = rows.filter({ has: page.locator('[title="fixture-activity-a-1"]') });
  await completed.getByText("已完成", { exact: true }).waitFor();
  assert.equal(await rows.count(), 25, "refresh inserts new tasks without exceeding page size");
  await page.getByRole("button", { name: "下一页任务", exact: true }).click();
  const pagination = page.getByRole("navigation", { name: "任务记录分页", exact: true });
  await pagination.getByText("第 2 页", { exact: true }).waitFor();
  await rows.nth(1).waitFor();
  assert.equal(await rows.count(), 2, "second page contains the remaining refreshed tasks");
  assert.equal(state.requests.filter(item => item.path.endsWith("/activity")).at(-1).query.offset, "25");
  await tab(page, "资源观测"); await tab(page, "操作记录");
  assert.equal(await pagination.getByText("第 2 页", { exact: true }).count(), 1, "task pagination survives tab changes");
  await selectOption(page, "操作功能", "通用助手");
  await pagination.getByText("第 1 页", { exact: true }).waitFor();
  await completed.getByText("已完成", { exact: true }).waitFor();
  assert.equal(await rows.count(), 1, "task module filter narrows the list");
  const query = state.requests.filter(item => item.path.endsWith("/activity")).at(-1).query;
  assert.equal(query.module, "assistant"); assert.equal(query.offset, "0");
  assert.equal(query.start, undefined, "task history has no resource date constraint");
  assert.equal(query.end, undefined, "task history has no resource date constraint");
  state.activityUpdates = false;
  await selectOption(page, "操作功能", "全部功能");
  await rows.nth(24).waitFor();
  await opener.click();
  await dialog.waitFor();
  // The source control is inert while the modal is open; simulate a parent
  // source change to verify that this lifecycle also closes the drawer.
  await page.getByRole("button", { name: "计次回执", exact: true, includeHidden: true }).evaluate(element => element.click());
  await dialog.waitFor({ state: "hidden" });
  await recordSource(page, "任务记录");
  assert.equal(await dialog.count(), 0, "task detail stays closed after switching sources back");
  await opener.click(); await dialog.waitFor();
  await page.getByRole("tab", { name: "次数总览", exact: true, includeHidden: true }).evaluate(element => element.click());
  await dialog.waitFor({ state: "hidden" });
  await tab(page, "操作记录");
  assert.equal(await dialog.count(), 0, "task detail stays closed after switching tabs back");
}
async function capture(page, viewport, scene, preserveScroll = false) {
  if (!preserveScroll) await page.locator(".np-usage-body").evaluate(element => { element.scrollTop = 0; });
  const path = resolve(output, `usage-${viewport.width}x${viewport.height}-${scene}.png`);
  await page.screenshot({ path, animations: "disabled" });
  report.screenshots.push(path);
}
async function verifyOperationMenu(page, viewport, suffix = "", source = "receipts") {
  const trigger = page.getByRole("combobox", { name: "操作功能", exact: true });
  await trigger.click();
  const menu = page.getByRole("listbox", { name: "操作功能", exact: true });
  const bounds = await menu.boundingBox();
  const minimumWidth = viewport.width >= 2000 && viewport.height >= 1120 ? 320 : 240;
  assert.ok(bounds.width >= minimumWidth - 1, "operation menu is wide enough for complete option labels");
  assert.ok(bounds.height >= 250, "operation menu shows several choices when viewport space is available");
  assert.ok(await menu.evaluate(element => element.scrollWidth <= element.clientWidth + 1), "operation menu has no horizontal label clipping");
  await capture(page, viewport, `operation-menu${suffix}`, true);
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.wheel(0, 10000);
  const menuId = source === "activity" ? "usage-activity-module-listbox" : "usage-operation-module-listbox";
  await page.waitForFunction(id => document.getElementById(id)?.scrollTop > 0, menuId, { timeout: 3000 });
  const lastOption = menu.getByRole("option", { name: source === "activity" ? "分子动力学演示" : "OpenScience", exact: true });
  const lastBounds = await lastOption.boundingBox();
  assert.ok(lastBounds.y >= bounds.y && lastBounds.y + lastBounds.height <= bounds.y + bounds.height + 1, "last long option is reachable by actual menu wheel scrolling");
  await lastOption.click();
  await page.getByText(source === "activity" ? "暂无任务记录。" : "暂无操作回执。", { exact: true }).waitFor();
  await trigger.click();
  const emptyBounds = await menu.boundingBox();
  assert.ok(emptyBounds.height >= 250, "an empty operations list does not collapse menu height");
  await capture(page, viewport, `empty-operation-menu${suffix}`, true);
  await menu.getByRole("option", { name: "分子图像识别", exact: true }).click();
  assert.equal(await trigger.innerText(), "分子图像识别");
  await selectOption(page, "操作功能", "全部功能");
  await page.getByRole("button", { name: source === "activity" ? "查看详情" : "查看回执", exact: true }).first().waitFor();
  return { width: bounds.width, height: bounds.height, emptyHeight: emptyBounds.height };
}
async function verifyResourceWheel(page, viewport, suffix = "") {
  const body = page.locator(".np-usage-body");
  const before = await body.evaluate(element => ({ top: element.scrollTop, height: element.clientHeight, scrollHeight: element.scrollHeight }));
  const bounds = await body.boundingBox();
  // Pointer input must reach the inner scrolling surface. Locator.click() or
  // scrollIntoView() would hide a broken wheel/scroll-chain implementation.
  await page.mouse.move(bounds.x + 8, Math.min(viewport.height - 50, bounds.y + 200));
  await page.mouse.wheel(0, 100000);
  await page.waitForFunction(() => document.querySelector('.np-usage-body')?.scrollTop > 0, null, { timeout: 3000 });
  const after = await body.evaluate(element => ({ top: element.scrollTop, height: element.clientHeight, scrollHeight: element.scrollHeight }));
  assert.ok(after.top > before.top, "actual resource wheel input scrolls the page body");
  assert.ok(after.height < after.scrollHeight, "resource body is constrained by the available viewport height");
  const coverage = await page.locator("#usage-panel-resources summary").filter({ hasText: /^统计范围$/ }).boundingBox();
  assert.ok(coverage.y >= bounds.y && coverage.y + coverage.height <= Math.min(viewport.height, bounds.y + bounds.height) + 1, "actual wheel reaches the final coverage disclosure");
  await capture(page, viewport, `resources-bottom${suffix}`, true);
  await body.evaluate(element => { element.scrollTop = 0; });
  return { before, after };
}
async function geometry(page, viewport) {
  const result = await page.evaluate(() => {
    const body = document.querySelector(".np-usage-body");
    const header = document.querySelector(".np-usage-page h1");
    const content = document.querySelector(".np-usage-content");
    const columns = selector => { const element = document.querySelector(selector); return element?.getBoundingClientRect().width ? getComputedStyle(element).gridTemplateColumns.split(" ").length : null; };
    return { pools: columns(".np-usage-pool-grid"), analysis: columns(".np-usage-analysis-grid"), kpis: columns(".np-usage-kpis"), content: content.getBoundingClientRect().width, contentMax: parseFloat(getComputedStyle(content).maxWidth), doc: document.documentElement.scrollWidth, viewport: innerWidth, body: body.getBoundingClientRect().width, header: header?.getBoundingClientRect().toJSON(), large: matchMedia("(min-width: 2000px) and (min-height: 1120px)").matches };
  });
  assert.ok(result.doc <= result.viewport + 1, "no document horizontal overflow");
  assert.ok(result.body > 200, "usable narrow layout");
  assert.equal(result.large, viewport.width >= 2000 && viewport.height >= 1120);
  assert.equal(result.contentMax, result.large ? 1560 : 1040, "content width follows the exact 2K breakpoint");
  assert.ok(result.content <= result.contentMax + 1, "content stays within the shared page width");
  if (result.pools !== null) assert.equal(result.pools, result.content >= 960 ? 4 : result.content >= 560 ? 2 : 1, "allowance columns follow the content container");
  if (viewport.width === 390 && result.kpis !== null) assert.equal(result.kpis, 1, "phone task summary is one column");
  return result;
}
async function waitHeld(page, state, key) {
  for (let i = 0; i < 100 && !state.releases.has(key); i++) await page.waitForTimeout(20);
  assert.ok(state.releases.has(key), `${key} request is in flight`);
}
function release(state, key) { state.releases.get(key)?.(); state.releases.delete(key); }
async function verifyBeijingResources(page, state, timezoneId) {
  assert.equal(await page.getByLabel("开始日期（北京时间）", { exact: true }).inputValue(), "2026-09-02");
  assert.equal(await page.getByLabel("结束日期（含当天）", { exact: true }).inputValue(), "2026-10-01", "default today is Beijing's date even when the browser is still September 30");
  const query = state.requests.filter(item => item.path.endsWith("/summary")).at(-1).query;
  assert.deepEqual(query, { start: "2026-09-01T16:00:00.000Z", end: "2026-10-01T16:00:00.000Z", timezone: "Asia/Shanghai" }, "summary requests Beijing calendar days encoded as UTC instants");
  const records = state.requests.filter(item => item.path.endsWith("/records")).at(-1).query;
  assert.equal(records.start, query.start); assert.equal(records.end, query.end);
  assert.equal(records.timezone, undefined, "record endpoint receives UTC instants without a timezone parameter");
  const table = page.getByRole("region", { name: "个人用量明细表，可横向滚动", exact: true });
  await table.locator("tbody tr").nth(1).waitFor();
  assert.equal(await table.getByRole("columnheader", { name: "提交时间（北京时间）", exact: true }).count(), 1);
  assert.equal(await table.locator("tbody tr").nth(0).locator("time").innerText(), "2026-09-30 23:59:59");
  assert.equal(await table.locator("tbody tr").nth(1).locator("time").innerText(), "2026-10-01 00:00:00");
  const trend = page.getByRole("region", { name: "每日任务趋势，可横向滚动", exact: true });
  assert.equal(await trend.getByRole("listitem", { name: "2026-09-30，25 个任务", exact: true }).count(), 1);
  assert.equal(await trend.getByRole("listitem", { name: "2026-10-01，1 个任务", exact: true }).count(), 1, "server Beijing buckets remain separate in the trend");
  assert.equal(await trend.getByRole("listitem").count(), 2);
  const beijing = summary("a", false, query), utc = summary("a", false, { ...query, timezone: "UTC" });
  assert.equal(beijing.period.timezone, "Asia/Shanghai");
  assert.deepEqual(beijing.daily.map(({ date, tasks }) => ({ date, tasks })), [{ date: "2026-09-30", tasks: 25 }, { date: "2026-10-01", tasks: 1 }]);
  assert.deepEqual(utc.daily.map(({ date, tasks }) => ({ date, tasks })), [{ date: "2026-09-30", tasks: 26 }], "fixture proves the Beijing buckets differ from UTC for identical instants");
  assert.equal(await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone), timezoneId, "browser timezone override is active");
  return { browserTimezone: timezoneId, fixedNow, query, daily: beijing.daily };
}
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (server && server.exitCode !== null) throw Error("Private preview exited: " + serverError);
    try { if ((await fetch(base, { signal: AbortSignal.timeout(1000) })).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, "private preview ready");
  browser = await chromium.launch({ headless: true, executablePath: process.env.PAGE_CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
  for (const [index, viewport] of viewports.entries()) {
    const timezoneId = browserTimezones[index];
    const context = await browser.newContext({ viewport, timezoneId, reducedMotion: "reduce", serviceWorkers: "block", permissions: ["clipboard-read", "clipboard-write"] });
    const state = await installFixture(context);
    const page = lastPage = await context.newPage(), errors = [];
    await page.clock.setFixedTime(fixedNow);
    page.setDefaultTimeout(10000);
    const checkpoint = step => { report.progress.push({ viewport, step }); console.log(JSON.stringify({ viewport, step })); };
    const refresh = () => page.getByRole("button", { name: "刷新全部", exact: true }).click();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base + "/account/usage");
    const pools = page.getByRole("region", { name: "分类成功次数", exact: true });
    await pools.getByRole("article").first().waitFor();
    assert.equal(await page.getByRole("heading", { name: "我的用量", exact: true }).count(), 1);
    assert.equal(await page.getByRole("tab", { name: "次数总览", exact: true }).getAttribute("aria-selected"), "true");
    assert.equal(await pools.getByRole("article").count(), 8);
    for (const label of Object.values(allowances.entitlements.policy.pool_labels)) assert.equal(await pools.getByRole("article", { name: label, exact: true }).count(), 1);
    await pools.getByText("当前为示例策略，不代表正式套餐承诺。", { exact: true }).waitFor();
    assert.equal(await pools.getByText(/当前.*周期（北京时间）.*2026-10-01 08:00:00.*2026-11-01 08:00:00/).count(), 1, "server entitlement period instants are displayed at Beijing 08:00 without changing the period");
    assert.equal(await pools.getByText(/另有 1 次旧周期预留/).count(), 1);
    await capture(page, viewport, "overview");
    const overviewGeometry = await geometry(page, viewport);
    await tab(page, "操作记录");
    await verifyActivity(page, state, viewport, index <= 1);
    checkpoint("task_activity_latest_attempt_detail_and_source");
    await recordSource(page, "计次回执");
    await page.getByRole("button", { name: "查看回执", exact: true }).first().waitFor();
    const receiptRows = page.getByRole("region", { name: "操作回执表，可横向滚动", exact: true }).locator("tbody tr");
    assert.equal(await receiptRows.nth(0).locator("time").innerText(), "2026-10-07 23:59:59");
    assert.equal(await receiptRows.nth(1).locator("time").innerText(), "2026-10-08 00:00:00", "receipt creation time uses Beijing's date boundary");
    const operationMenu = await verifyOperationMenu(page, viewport);
    await capture(page, viewport, "operations");
    const opener = page.getByRole("button", { name: "查看回执", exact: true }).first();
    await opener.click();
    const dialog = page.getByRole("dialog");
    await dialog.getByText(allowances.operations.items[0].operation_id, { exact: true }).waitFor();
    assert.equal(await dialog.getByText("2026-10-07 23:59:59", { exact: true }).count(), 1);
    assert.equal(await dialog.getByText("2026-10-08 00:00:01", { exact: true }).count(), 1);
    assert.equal(await dialog.getByText(/2026-10-01 08:00:00 至 2026-11-01 08:00:00/).count(), 1, "receipt detail preserves the server period while converting its display");
    const drawerBox = await dialog.boundingBox();
    const expectedDrawerWidth = viewport.width < 560 ? viewport.width : viewport.width >= 2000 && viewport.height >= 1120 ? 540 : 380;
    assert.ok(Math.abs(drawerBox.width - expectedDrawerWidth) <= 1, "drawer follows standard, narrow and 2K widths");
    await capture(page, viewport, "receipt");
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    await page.waitForFunction(() => document.activeElement?.textContent === "查看回执");
    await tab(page, "资源观测");
    await page.getByRole("heading", { name: "已观测指标", exact: true }).waitFor();
    const beijingDates = await verifyBeijingResources(page, state, timezoneId);
    await capture(page, viewport, "resources");
    const resourceWheel = await verifyResourceWheel(page, viewport);
    const resourceGeometry = await geometry(page, viewport);
    assert.deepEqual(resourceGeometry.header, overviewGeometry.header, "header geometry is stable across tabs");
    checkpoint("three_tabs_drawer_responsive_screenshots");
    if (index > 1) {
      assert.deepEqual(errors, [], "no uncaught browser errors");
      report.results.push({ viewport, status: "PASS", checks: ["beijing_midnight_all_record_views", "beijing_default_and_request_bounds", "beijing_trend_buckets", "browser_timezone_independence", "server_entitlement_period_preserved", "three_tabs", "task_activity_default", "task_states_latest_attempt", "task_detail_no_request", "task_drawer_width_escape_focus", "task_menu_width_empty_height_and_last_option", "drawer_escape_focus", "drawer_width", "no_overflow", "2k_boundary", "resource_actual_wheel_to_bottom", "operation_menu_width_empty_height_and_last_option"], beijingDates, geometry: { overview: overviewGeometry, resources: resourceGeometry, operationMenu, resourceWheel } });
      await context.close(); continue;
    }
    assert.ok(await page.getByText("未记录", { exact: true }).count());
    const firstSummary = state.requests.find(item => item.path.endsWith("/summary")).query;
    assert.equal((Date.parse(firstSummary.end) - Date.parse(firstSummary.start)) / 86400000, 30);
    await page.locator("summary").filter({ hasText: /^统计范围$/ }).click();
    const coverage = page.getByRole("region", { name: "统计范围", exact: true });
    assert.equal(await coverage.getByRole("listitem").count(), 22);
    assert.equal(await coverage.getByText("部分计量", { exact: true }).count(), 21);
    assert.equal(await coverage.getByText("外部服务", { exact: true }).count(), 1);
    for (const item of catalog.modules) assert.equal(await coverage.getByText(item.label, { exact: true }).count(), 1);
    await page.locator("summary").filter({ hasText: /^全部指标/ }).click();
    const metricCards = page.getByRole("region", { name: "已观测指标", exact: true });
    for (const metric of catalog.metrics) assert.equal(await metricCards.getByText(metric.label, { exact: true }).count(), 1);
    const moduleTable = page.getByRole("region", { name: "按功能用量表，可横向滚动", exact: true });
    await moduleTable.getByText("1.2 秒", { exact: true }).waitFor();
    assert.equal(await moduleTable.getByText("25 条已记录 · 1 条未记录", { exact: true }).count(), 1);
    assert.equal(await moduleTable.getByText("1 条已记录 · 25 条未记录", { exact: true }).count(), 1);
    assert.equal(await page.getByText("执行尝试", { exact: true }).count(), 0);
    assert.equal(await page.getByText(/根任务行仅显示自身记录的指标，不聚合子调用/).count(), 1);
    assert.equal(await metricCards.getByText(/不可与供应商总 Tokens 叠加/).count(), 1);
    checkpoint("full_catalog_metrics_and_module_summary");
    await page.getByRole("tab", { name: "资源观测", exact: true }).focus();
    await page.keyboard.press("Home");
    assert.equal(await page.getByRole("tab", { name: "次数总览", exact: true }).getAttribute("aria-selected"), "true");
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.getByRole("tab", { name: "操作记录", exact: true }).getAttribute("aria-selected"), "true");
    await page.keyboard.press("End");
    assert.equal(await page.getByRole("tab", { name: "资源观测", exact: true }).getAttribute("aria-selected"), "true");
    await page.keyboard.press("ArrowLeft");
    assert.equal(await page.getByRole("tab", { name: "操作记录", exact: true }).getAttribute("aria-selected"), "true");
    await page.getByRole("button", { name: "读取已有结果", exact: true }).first().click();
    await dialog.getByText("已有结果已读取，未重新执行任务。", { exact: true }).waitFor();
    await page.keyboard.press("Escape"); await dialog.waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "读取已有结果", exact: true }).nth(1).click();
    await dialog.getByRole("alert").filter({ hasText: "操作结果待确认" }).waitFor();
    const unknownReads = state.requests.filter(item => item.path.endsWith(allowances.operations.items[1].operation_id + "/result")).length;
    await capture(page, viewport, "unknown-result");
    await page.waitForTimeout(100);
    assert.equal(state.requests.filter(item => item.path.endsWith(allowances.operations.items[1].operation_id + "/result")).length, unknownReads, "no automatic result or compute retry");
    await page.keyboard.press("Escape"); await dialog.waitFor({ state: "hidden" });
    state.hold.add("result"); await page.getByRole("button", { name: "读取已有结果", exact: true }).first().click();
    await waitHeld(page, state, "result");
    // Simulates a parent tab change while a modal owns focus; no result may leak to another context.
    await page.getByRole("tab", { name: "次数总览", exact: true, includeHidden: true }).evaluate(element => element.click());
    release(state, "result"); await dialog.waitFor({ state: "hidden" });
    await tab(page, "操作记录");
    assert.equal(await dialog.count(), 0);
    state.allowanceDisabled = true; await refresh(); await tab(page, "次数总览");
    await pools.getByText(/分类次数尚未启用/).waitFor();
    await capture(page, viewport, "allowance-disabled");
    await tab(page, "操作记录");
    await recordSource(page, "任务记录");
    await page.getByRole("region", { name: "任务记录表，可横向滚动", exact: true }).locator("tbody tr").nth(24).waitFor();
    assert.equal(await page.getByRole("button", { name: "查看详情", exact: true }).count(), 25, "task history remains available with entitlement enforcement disabled");
    await recordSource(page, "计次回执");
    assert.equal(await page.getByRole("button", { name: "读取已有结果", exact: true }).count(), 2, "durable history remains readable with admission off");
    await verifyOperationMenu(page, viewport, "-allowance-disabled");
    state.operationUpdates = true; await refresh();
    await page.getByRole("button", { name: "查看回执", exact: true }).nth(2).waitFor();
    const updatedRows = page.getByRole("region", { name: "操作回执表，可横向滚动", exact: true }).locator("tbody tr");
    assert.equal(await updatedRows.nth(1).getByText("已计成功次数", { exact: true }).count(), 1, "refresh shows an operation's completed state");
    assert.equal(await updatedRows.nth(2).locator('[title="00000000-0000-4000-8000-000000000003"]').count(), 1, "refresh shows newly created operation rows");
    state.operationUpdates = false; state.allowanceDisabled = false; state.paginatedOperations = true; await refresh();
    await page.getByRole("button", { name: "下一页操作", exact: true }).click();
    await page.getByRole("navigation", { name: "操作回执分页" }).getByText("第 2 页", { exact: true }).waitFor();
    await tab(page, "次数总览"); await tab(page, "操作记录");
    assert.equal(await page.getByRole("navigation", { name: "操作回执分页" }).getByText("第 2 页", { exact: true }).count(), 1);
    await selectOption(page, "操作功能", "分子动力学");
    await page.getByRole("navigation", { name: "操作回执分页" }).getByText("第 1 页", { exact: true }).waitFor();
    assert.equal(state.requests.filter(item => item.path.endsWith("/operations")).at(-1).query.module, "monomer_md");
    await tab(page, "资源观测"); await tab(page, "操作记录");
    assert.equal(await page.getByRole("combobox", { name: "操作功能", exact: true }).innerText(), "分子动力学");
    checkpoint("operation_context_pagination_durable_results_and_tab_keyboard");
    await recordSource(page, "任务记录");
    const activityTable = page.getByRole("region", { name: "任务记录表，可横向滚动", exact: true });
    await activityTable.locator("tbody tr").nth(24).waitFor();
    state.activityMode = "error"; await refresh();
    await page.getByRole("alert").filter({ hasText: "任务记录暂时无法读取" }).waitFor();
    assert.equal(await activityTable.locator("tbody tr").count(), 25, "task refresh errors preserve the previously read list");
    await capture(page, viewport, "activity-error");
    await recordSource(page, "计次回执");
    await page.getByRole("button", { name: "查看回执", exact: true }).first().waitFor();
    assert.equal(await page.getByRole("alert").filter({ hasText: "任务记录暂时无法读取" }).count(), 0, "task errors stay inside their source");
    await tab(page, "次数总览"); assert.equal(await pools.getByRole("article").count(), 8, "task errors do not hide allowances");
    await tab(page, "操作记录"); await recordSource(page, "任务记录");
    state.activityMode = "disabled"; await refresh();
    await page.getByRole("heading", { name: "任务记录尚未启用", exact: true }).waitFor();
    await capture(page, viewport, "activity-disabled");
    await recordSource(page, "计次回执");
    await page.getByRole("button", { name: "查看回执", exact: true }).first().waitFor();
    assert.equal(await page.getByRole("heading", { name: "任务记录尚未启用", exact: true }).count(), 0, "metering-disabled task history does not hide durable receipts");
    await recordSource(page, "任务记录");
    state.activityMode = "normal";
    await page.getByRole("button", { name: "重新检查任务记录", exact: true }).click();
    await activityTable.locator("tbody tr").nth(24).waitFor();
    await recordSource(page, "计次回执");
    checkpoint("independent_task_error_disabled_and_retry");
    if (viewport.width < 1024) await page.getByRole("button", { name: "打开导航", exact: true }).click();
    await page.getByRole("button", { name: "账号菜单：测试账号a", exact: true }).last().click();
    await page.getByRole("menuitem", { name: "我的用量", exact: true }).click();
    await tab(page, "资源观测");
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await page.getByText("共 26 条 · 第 2 / 2 页", { exact: true }).waitFor();
    await tab(page, "次数总览"); await tab(page, "资源观测");
    assert.equal(await page.getByText("共 26 条 · 第 2 / 2 页", { exact: true }).count(), 1);
    const beforePresets = state.requests.length;
    for (const [days, start] of [["7", "2026-09-25"], ["90", "2026-07-04"], ["30", "2026-09-02"]]) {
      await page.getByLabel("快捷日期范围", { exact: true }).selectOption(days);
      assert.equal(await page.getByLabel("开始日期（北京时间）", { exact: true }).inputValue(), start);
      assert.equal(await page.getByLabel("结束日期（含当天）", { exact: true }).inputValue(), "2026-10-01", "all presets include Beijing today across system timezones");
    }
    assert.equal(state.requests.length, beforePresets, "editing quick date presets waits for explicit application");
    await page.getByLabel("开始日期（北京时间）", { exact: true }).fill("2023-01-01");
    await page.getByLabel("结束日期（含当天）", { exact: true }).fill("2024-02-29");
    const beforeInvalid = state.requests.length;
    await page.getByRole("button", { name: "应用筛选", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "366" }).waitFor();
    assert.equal(state.requests.length, beforeInvalid, "invalid date interval does not fetch");
    await page.getByLabel("开始日期（北京时间）", { exact: true }).fill("2024-02-01");
    await selectOption(page, "功能", "分子动力学");
    await page.getByText("筛选尚未应用", { exact: true }).waitFor();
    await page.getByRole("button", { name: "应用筛选", exact: true }).click();
    await page.getByText("共 26 条 · 第 1 / 2 页", { exact: true }).waitFor();
    const filtered = state.requests.filter(item => item.path.endsWith("/summary")).at(-1).query;
    assert.equal(filtered.start, "2024-01-31T16:00:00.000Z"); assert.equal(filtered.end, "2024-02-29T16:00:00.000Z"); assert.equal(filtered.module, "monomer_md"); assert.equal(filtered.timezone, "Asia/Shanghai");
    assert.equal((Date.parse(filtered.end) - Date.parse(filtered.start)) / 86400000, 29, "inclusive leap-February range spans 29 Beijing dates");
    await selectOption(page, "明细状态", "失败");
    await page.getByLabel("显示子调用明细", { exact: true }).check();
    await page.getByText("共 1 条 · 第 1 / 1 页", { exact: true }).waitFor();
    assert.equal(state.requests.filter(item => item.path.endsWith("/records")).at(-1).query.include_children, "true");
    assert.equal(state.requests.filter(item => item.path.endsWith("/records")).at(-1).query.timezone, undefined, "detail filters never forward the summary timezone parameter");
    await tab(page, "次数总览"); await tab(page, "资源观测");
    assert.equal(await page.getByLabel("开始日期（北京时间）", { exact: true }).inputValue(), "2024-02-01");
    assert.equal(await page.getByRole("combobox", { name: "明细状态", exact: true }).innerText(), "失败");
    state.hold.add("summary"); state.hold.add("entitlements");
    const beforeRefresh = state.requests.length;
    await refresh(); await waitHeld(page, state, "summary"); await waitHeld(page, state, "entitlements");
    assert.equal(await page.getByRole("region", { name: "用量概览", exact: true }).getByText("26", { exact: true }).count(), 1, "same-query refresh keeps summary visible");
    await tab(page, "次数总览"); assert.equal(await pools.getByRole("article").count(), 8, "same-query refresh keeps balances visible");
    const allRefreshEndpoints = ["catalog", "summary", "records", "entitlements", "operations", "activity"];
    for (let i = 0; i < 100 && !allRefreshEndpoints.every(endpoint => state.requests.slice(beforeRefresh).some(item => item.path.endsWith("/" + endpoint))); i++) await page.waitForTimeout(20);
    for (const endpoint of allRefreshEndpoints) assert.ok(state.requests.slice(beforeRefresh).some(item => item.path.endsWith("/" + endpoint)), `refresh all includes ${endpoint}`);
    release(state, "summary"); release(state, "entitlements");
    await tab(page, "资源观测");
    assert.deepEqual(state.requests.filter(item => item.path.endsWith("/summary")).at(-1).query, filtered);
    checkpoint("inclusive_beijing_dates_max366_filter_context_and_stable_refresh_all");
    state.mode = "error"; await refresh();
    await page.getByRole("alert").filter({ hasText: "统计汇总暂时无法读取" }).waitFor();
    await capture(page, viewport, "summary-error");
    assert.equal(await page.getByText("共 1 条 · 第 1 / 1 页", { exact: true }).count(), 1, "summary failure does not hide records");
    state.mode = "disabled"; await refresh();
    await page.getByRole("heading", { name: "用量统计尚未启用", exact: true }).waitFor();
    await capture(page, viewport, "resource-disabled");
    await tab(page, "次数总览"); assert.equal(await pools.getByRole("article").count(), 8, "resource unavailable does not hide allowances");
    await tab(page, "资源观测");
    state.mode = "empty"; await page.getByRole("button", { name: "重新检查", exact: true }).click();
    await page.getByText("所选范围内暂无已记录的任务。", { exact: true }).waitFor();
    checkpoint("independent_error_disabled_and_empty");
    state.mode = "normal"; state.hold.add("summary"); state.hold.add("activity");
    await refresh(); await waitHeld(page, state, "summary"); await waitHeld(page, state, "activity");
    await tab(page, "操作记录"); await recordSource(page, "任务记录");
    await page.getByRole("button", { name: "查看详情", exact: true }).first().click();
    await page.getByRole("dialog", { name: "任务记录详情", exact: true }).locator(".np-usage-drawer-id code").filter({ hasText: /^fixture-activity-a-0$/ }).waitFor();
    state.owner = "b"; await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForFunction(() => document.querySelector('#usage-tab-overview')?.getAttribute("aria-selected") === "true");
    assert.equal(await page.getByRole("dialog").count(), 0, "account switch closes the prior task detail");
    await tab(page, "资源观测");
    await page.getByRole("region", { name: "用量概览", exact: true }).getByText("7", { exact: true }).first().waitFor();
    release(state, "summary"); release(state, "activity"); await page.waitForTimeout(100);
    assert.equal(await page.getByRole("region", { name: "用量概览", exact: true }).getByText("26", { exact: true }).count(), 0);
    await tab(page, "操作记录");
    const newOwnerTasks = page.getByRole("region", { name: "任务记录表，可横向滚动", exact: true });
    await newOwnerTasks.locator('[title="fixture-activity-b-0"]').waitFor();
    assert.equal(await newOwnerTasks.locator("tbody tr").count(), 7);
    assert.equal(await page.locator('[title^="fixture-activity-a-"]').count(), 0, "late retired-account activity never returns to the task table");
    assert.equal(await page.getByRole("dialog").count(), 0, "late retired-account activity cannot reopen a task detail");
    checkpoint("account_late_summary_and_activity_response");
    state.plan = "free"; await page.goto(base + "/account/usage");
    await pools.getByText("免费账号", { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('button[aria-label="刷新全部"]')?.disabled === false);
    assert.equal(await page.locator('iframe[src*="workspace.ci.invalid"]').count(), 0, "free account must not mount shared workspace");
    await tab(page, "资源观测");
    await page.getByRole("heading", { name: "已观测指标", exact: true }).waitFor();
    await verifyResourceWheel(page, viewport, "-free-account");
    checkpoint("free_account_resource_actual_wheel");
    const beforeGuest = state.requests.length; state.owner = null;
    await page.goto(base + "/account/usage"); await page.getByRole("heading", { name: "登录后查看我的用量", exact: true }).waitFor();
    assert.equal(state.requests.length, beforeGuest, "guest performs no usage fetch");
    assert.deepEqual(errors, [], "no uncaught browser errors");
    report.results.push({ viewport, status: "PASS", checks: ["beijing_midnight_all_record_views", "beijing_default_and_request_bounds", "beijing_trend_buckets", "browser_timezone_independence", "server_entitlement_period_preserved", "three_tabs", "task_activity_default", "task_states_latest_attempt", "task_detail_and_clipboard_no_request", "task_drawer_width_escape_focus", "task_menu_width_empty_height_and_last_option", "task_pagination25_and_module_filter", "task_refresh_completed_and_new_row", "task_source_and_tab_close_drawer", "task_error_disabled_independent", "task_retry", "task_late_account_isolation", "resource_actual_wheel_to_bottom", "operation_menu_width_empty_height_and_last_option", "disabled_operation_menu", "refresh_completed_state_and_new_row", "free_account_actual_wheel", "eight_pools", "example_policy", "old_period_hold", "GET_saved_result", "unknown_no_auto_retry", "tab_abort_result", "drawer_escape_focus", "drawer_width", "operation_pagination", "tab_keyboard", "off_keeps_durable_history", "free_permissions", "free_no_shared_iframe", "full21_catalog_plus_external", "all9_metrics", "module_partial_execution_root_wall", "attempt_and_token_semantics", "account_navigation", "default30_beijing", "unknown_metrics", "pagination", "inclusive_beijing_dates", "max366", "module_filter", "status_child_filters", "filter_page_preserved", "refresh_all_six_endpoints", "stable_refresh", "narrow_layout", "independent_summary_error", "disabled503", "empty", "late_account_response", "guest_no_fetch"], beijingDates, geometry: { overview: overviewGeometry, resources: resourceGeometry, operationMenu, resourceWheel } });
    await context.close();
  }
  assert.deepEqual(report.unexpected, [], "all undeclared network is a failure");
} catch (error) {
  report.failures.push(error instanceof Error ? error.stack : String(error)); process.exitCode = 1;
  if (lastPage && !lastPage.isClosed()) {
    await lastPage.screenshot({ path: resolve(output, "failure.png"), animations: "disabled" }).catch(() => {});
    await writeFile(resolve(output, "failure-body.txt"), await lastPage.locator("body").innerText().catch(() => "")).catch(() => {});
  }
} finally {
  await browser?.close();
  if (server?.pid) { try { process.kill(-server.pid, "SIGTERM"); } catch {} }
  report.status = report.failures.length ? "FAIL" : "PASS";
  await writeFile(resolve(output, "personal-usage-browser.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report));
}
