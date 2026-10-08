// Real browser, fixture API, private loopback preview. No live account/server/AI/GPU.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
const { chromium } = await import(process.env.PAGE_PLAYWRIGHT_MODULE || "playwright");
const base = "http://127.0.0.1:5297";
const output = resolve(process.env.PAGE_ARTIFACT_DIR || "/tmp/nexpoly-personal-usage");
await mkdir(output, { recursive: true });
const server = spawn("npm", ["run", "preview", "--", "--host", "127.0.0.1", "--port", "5297", "--strictPort"], {
  detached: true, stdio: ["ignore", "ignore", "pipe"], env: { ...process.env, DEV_PROXY_TARGET: "http://127.0.0.1:9" }
});
let serverError = ""; server.stderr.on("data", data => { serverError = (serverError + data).slice(-2000); });
const report = { scope: "isolated real UI with synthetic API fixtures; not server ownership acceptance", results: [], progress: [], blocked: [], unexpected: [], failures: [] };
let browser;
const catalog = { enabled: true, mode: "statistics_only", retention_days: 365,
  modules: [{ id: "md", label: "单体 MD", category: "compute", coverage: "measured" }, { id: "assistant", label: "AI 助手", category: "ai", coverage: "partial", note: "仅记录提供方返回的指标" }],
  metrics: [{ id: "execution_ms", label: "执行耗时", unit: "ms" }, { id: "provider_total_tokens", label: "总令牌", unit: "tokens" }, { id: "artifact_bytes", label: "体积", unit: "bytes" }] };
function session(owner) { return { authenticated: !!owner, user: owner ? { id: `fixture-owner-${owner}`, username: `测试账号${owner}`, must_change_password: false } : null,
  session_id: owner ? `fixture-session-${owner}` : null, csrf_token: owner ? "fixture-only-csrf" : null, capabilities: {} }; }
function summary(owner, empty = false) {
  const tasks = empty ? 0 : owner === "b" ? 7 : 26;
  return { period: { start: "2026-09-01T00:00:00.000Z", end: "2026-10-01T00:00:00.000Z", timezone: "UTC", basis: "task_submitted_at" },
    totals: { tasks, attempts: tasks, child_calls: 0, states: { completed: tasks }, metrics: { execution_ms: { value: 0, known_count: tasks, unknown_count: 0 }, provider_total_tokens: { value: null, known_count: 0, unknown_count: tasks } } },
    modules: [], daily: empty ? [] : [{ date: "2026-09-30", tasks, states: { completed: tasks } }], coverage: { uncovered: ["assistant"], warnings: ["部分历史任务没有计量记录"] } };
}
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw Error("Private preview exited: " + serverError);
    try { if ((await fetch(base, { signal: AbortSignal.timeout(1000) })).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, "private preview ready");
  browser = await chromium.launch({ headless: true, executablePath: process.env.PAGE_CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
    const state = { owner: "a", mode: "normal", requests: [], hold: false, release: null };
    await context.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== base) {
        const expected = ["https://workspace.ci.invalid", "https://fonts.googleapis.com", "https://fonts.gstatic.com"].includes(url.origin);
        report.blocked.push({ origin: url.origin, category: expected ? "excluded_external_workspace_or_font" : "unexpected_origin" });
        if (!expected) report.unexpected.push(url.origin);
        return route.abort();
      }
      if (!url.pathname.startsWith("/api/")) {
        if (!["GET", "HEAD"].includes(request.method())) { report.unexpected.push(request.method() + " static"); return route.abort(); }
        return route.continue();
      }
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body), headers: { "Cache-Control": "no-store" } });
      if (url.pathname === "/api/v1/auth/session" && request.method() === "GET") return json(session(state.owner));
      if (request.method() !== "GET" || !["/api/v1/usage/catalog", "/api/v1/usage/summary", "/api/v1/usage/records"].includes(url.pathname)) {
        report.unexpected.push(request.method() + " " + url.pathname); return json({ detail: "Blocked unexpected fixture API" }, 503);
      }
      assert.ok(state.owner, "guest must not query private usage");
      assert.equal(request.headers()["x-session-context"], `fixture-session-${state.owner}`);
      assert.equal(url.searchParams.has("owner_user_id"), false);
      state.requests.push({ path: url.pathname, query: Object.fromEntries(url.searchParams) });
      if (url.pathname.endsWith("/catalog")) return json(catalog);
      if (url.pathname.endsWith("/summary")) {
        if (state.mode === "disabled") return json({ code: "usage_metering_disabled", detail: "disabled" }, 503);
        const body = summary(state.owner, state.mode === "empty");
        if (state.hold) { state.hold = false; await new Promise(resolve => { state.release = resolve; }); }
        try { return await json(body); } catch { return; } // Retired request can be aborted by privateFetch.
      }
      const page = Number(url.searchParams.get("page"));
      const all = state.mode === "empty" ? [] : Array.from({ length: state.owner === "b" ? 7 : 26 }, (_, i) => ({ module: "md", task_id: `fixture-${state.owner}-${i}`, attempt_id: `fixture-attempt-${i}`, kind: "task", status: i === 0 ? "failed" : "completed", submitted_at: "2026-09-30T01:02:03.000Z", started_at: null, finished_at: null, parent_task_id: null, root_task_id: `fixture-${state.owner}-${i}`, source: "fixture", metrics: { execution_ms: 0, provider_total_tokens: null, artifact_bytes: null } }));
      const matching = url.searchParams.has("status") ? all.filter(item => item.status === url.searchParams.get("status")) : all;
      return json({ items: matching.slice((page - 1) * 25, page * 25), page, page_size: 25, total: matching.length });
    });
    const page = await context.newPage(); const errors = [];
    const checkpoint = step => { report.progress.push({ width: viewport.width, step }); console.log(JSON.stringify({ width: viewport.width, step })); };
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base + "/account/usage");
    await page.getByRole("heading", { name: "已观测指标", exact: true }).waitFor();
    assert.equal(await page.getByRole("heading", { name: "我的用量", exact: true }).count(), 1);
    assert.ok(await page.getByText("未记录", { exact: true }).count());
    const firstSummary = state.requests.find(item => item.path.endsWith("/summary")).query;
    assert.equal((Date.parse(firstSummary.end) - Date.parse(firstSummary.start)) / 86400000, 30);
    checkpoint("initial_render");
    await page.screenshot({ path: resolve(output, `usage-${viewport.width}.png`) });
    if (viewport.width < 1024) await page.getByRole("button", { name: "打开导航", exact: true }).click();
    await page.getByRole("button", { name: "账号菜单：测试账号a", exact: true }).last().click();
    await page.getByRole("menuitem", { name: "我的用量", exact: true }).click();
    await page.getByRole("heading", { name: "已观测指标", exact: true }).waitFor();
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await page.getByText("共 26 条 · 第 2 / 2 页", { exact: true }).waitFor();
    await page.getByLabel("开始日期（UTC）", { exact: true }).fill("2024-02-01");
    await page.getByLabel("结束日期（含当天）", { exact: true }).fill("2024-02-29");
    await page.getByLabel("功能", { exact: true }).selectOption("md");
    await page.getByRole("button", { name: "应用筛选", exact: true }).click();
    await page.getByText("共 26 条 · 第 1 / 2 页", { exact: true }).waitFor();
    checkpoint("navigation_pagination_dates");
    const filtered = state.requests.filter(item => item.path.endsWith("/summary")).at(-1).query;
    assert.equal(filtered.start, "2024-02-01T00:00:00.000Z"); assert.equal(filtered.end, "2024-03-01T00:00:00.000Z"); assert.equal(filtered.module, "md");
    await page.getByLabel("明细状态", { exact: true }).selectOption("failed");
    await page.getByLabel("显示子调用明细", { exact: true }).check();
    await page.getByText("共 1 条 · 第 1 / 1 页", { exact: true }).waitFor();
    assert.equal(state.requests.filter(item => item.path.endsWith("/records")).at(-1).query.include_children, "true");
    const geometry = await page.evaluate(() => ({ doc: document.documentElement.scrollWidth, viewport: innerWidth, body: document.querySelector(".np-usage-body").getBoundingClientRect().width }));
    assert.ok(geometry.doc <= geometry.viewport + 1, "no document horizontal overflow");
    assert.ok(geometry.body > 200, "usable narrow layout");
    checkpoint("filters_and_geometry");
    state.mode = "disabled"; await page.getByRole("button", { name: "刷新用量", exact: true }).click();
    await page.getByRole("heading", { name: "用量统计尚未启用", exact: true }).waitFor();
    state.mode = "empty"; await page.getByRole("button", { name: "重新检查", exact: true }).click();
    await page.getByText("所选范围内暂无已记录的任务。", { exact: true }).waitFor();
    checkpoint("disabled_and_empty");
    state.mode = "normal"; state.hold = true; await page.getByRole("button", { name: "刷新用量", exact: true }).click();
    for (let i = 0; i < 100 && !state.release; i++) await page.waitForTimeout(20);
    assert.ok(state.release, "old account summary is in flight");
    state.owner = "b"; await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByRole("region", { name: "用量概览", exact: true }).getByText("7", { exact: true }).first().waitFor();
    state.release(); await page.waitForTimeout(100);
    assert.equal(await page.getByRole("region", { name: "用量概览", exact: true }).getByText("26", { exact: true }).count(), 0);
    checkpoint("account_late_response");
    const beforeGuest = state.requests.length; state.owner = null;
    await page.goto(base + "/account/usage"); await page.getByRole("heading", { name: "登录后查看我的用量", exact: true }).waitFor();
    assert.equal(state.requests.length, beforeGuest, "guest performs no usage fetch");
    assert.deepEqual(errors, [], "no uncaught browser errors");
    report.results.push({ viewport, status: "PASS", checks: ["account_navigation", "default30utc", "unknown_metrics", "pagination", "inclusive_utc_dates", "module_filter", "status_child_filters", "narrow_layout", "disabled503", "empty", "late_account_response", "guest_no_fetch"], geometry });
    await context.close();
  }
  assert.deepEqual(report.unexpected, [], "all undeclared network is a failure");
} catch (error) { report.failures.push(String(error)); process.exitCode = 1; }
finally {
  await browser?.close();
  if (server.pid) { try { process.kill(-server.pid, "SIGTERM"); } catch {} }
  report.status = report.failures.length ? "FAIL" : "PASS";
  await writeFile(resolve(output, "personal-usage-browser.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report));
}
