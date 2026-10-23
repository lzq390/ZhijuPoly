// Runs against the actual handoff application and its in-app Mock.
// Network routing only rejects external traffic: business responses are never replaced.
import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { inspectZip } from "./handoff-archive.mjs";
import { chromium } from "playwright";
import { handoffPageReady } from "./handoff-readiness.mjs";

const base = new URL(process.env.HANDOFF_BASE_URL || "http://127.0.0.1:5173");
assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(base.hostname), "Use a local handoff server");
const output = resolve(process.env.HANDOFF_ARTIFACT_DIR || "handoff-verification");
const timeout = Number(process.env.HANDOFF_TIMEOUT_MS || 30000);
const selected = name => !process.env.HANDOFF_GROUP || process.env.HANDOFF_GROUP.split(",").includes(name);
const modules = [
  ["/structure-workbench", "结构工作台"], ["/knowledge", "知识检索"],
  ["/polytao-generation", "聚合物生成"], ["/monomer-retrosynthesis", "单体逆合成反推"],
  ["/explorer", "聚合物相似性探索"], ["/database-query", "数据库查询"],
  ["/database-filter", "数据库筛选"], ["/database", "数据库分析"],
  ["/homopolymer-property-prediction", "均聚物性质预测"], ["/monomer-polymerization", "单体正向聚合"],
  ["/md-simulation", "MD 模拟"], ["/monomer-md-simulation", "单体 MD 模拟"],
  ["/monomer-dft", "单体 DFT"], ["/conditional-generation", "条件聚合物生成"],
  ["/reverse-design", "Tg 逆向设计"], ["/high-throughput-workflow-demo", "高通量优化演示"],
].filter(([path]) => !process.env.HANDOFF_PATH || process.env.HANDOFF_PATH.split(",").includes(path));
const viewports = [[390, 844], [1440, 900], [2560, 1440]]
  .filter(([width]) => !process.env.HANDOFF_VIEWPORT || process.env.HANDOFF_VIEWPORT.split(",").includes(String(width)));
const scenarios = ["success", "loading", "empty", "error"]
  .filter(value => !process.env.HANDOFF_SCENARIO || process.env.HANDOFF_SCENARIO.split(",").includes(value));
assert.ok(modules.length && viewports.length && scenarios.length, "Filters must select at least one matrix case");
const routePaths = ["/", "/database/process", "/database/property", "/database/structure-effect", "/database/dft",
      "/database/formulation", "/lab-data", "/lab-data/collect", "/lab-data/dashboard", "/experiment-workflow-demo",
      "/conditional-generation/polytao", "/database/property-filter"];
const expectedTotal = (selected("matrix") ? modules.length * viewports.length * scenarios.length : 0) +
  (selected("routes") ? routePaths.length : 0) + (selected("auth") ? 1 : 0) +
  (selected("editor") ? 1 : 0) + (selected("batch") ? 1 : 0) + (selected("streams") ? 2 : 0);
assert.ok(expectedTotal > 0, "Filters must select at least one known verification group");
await mkdir(output, { recursive: true });
const report = { base: base.origin, startedAt: new Date().toISOString(), mockTransport: "application", expectedTotal, results: [] };
const browser = await chromium.launch({ headless: true,
  executablePath: process.env.HANDOFF_CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--enable-unsafe-swiftshader"] });
const urlFor = (path, scenario = "success", auth = "authenticated") => {
  const url = new URL(path, base);
  url.searchParams.set("mockScenario", scenario);
  url.searchParams.set("mockAuth", auth);
  return url.href;
};
const slug = value => value.replace(/[^a-zA-Z0-9-]/g, "-");


// Observe both native fetch and the handoff installer assigning globalThis.fetch.
// The wrapper returns the original response without consuming or rewriting its body.
function installAudit() {
  const audit = { fetches: [], violations: [] };
  window.__handoffBrowserAudit = audit;
  const wrapped = new WeakSet();
  const wrap = implementation => {
    if (wrapped.has(implementation)) return implementation;
    const observed = async function (...args) {
      const [input, init] = args;
      const request = input instanceof Request ? input : null;
      const entry = { url: new URL(request?.url ?? String(input), location.href).href,
        method: init?.method ?? request?.method ?? "GET", started: performance.now(),
        hasAdmissionKey: new Headers(init?.headers ?? request?.headers).has("Idempotency-Key") };
      audit.fetches.push(entry);
      try {
        const response = await Reflect.apply(implementation, this, args);
        entry.status = response.status;
        entry.contentType = response.headers.get("content-type");
        entry.responded = performance.now();
        return response;
      } catch (error) {
        entry.error = error?.name ?? String(error);
        throw error;
      }
    };
    wrapped.add(observed);
    return observed;
  };
  let implementation = wrap(globalThis.fetch.bind(globalThis));
  Object.defineProperty(globalThis, "fetch", { configurable: true,
    get: () => implementation, set: value => { implementation = wrap(value); } });
  document.addEventListener("securitypolicyviolation", event => {
    audit.violations.push({ directive: event.effectiveDirective, blockedURI: event.blockedURI });
  });
}

async function run(name, options, execute) {
  const { width = 1440, height = 900 } = options;
  const result = { name, ...options, passed: false, errors: [], externalRequests: [],
    escapedApiRequests: [], missingResources: [], websocketUrls: [] };
  const context = await browser.newContext({ viewport: { width, height }, reducedMotion: "reduce",
    acceptDownloads: true, permissions: ["clipboard-read", "clipboard-write"] });
  await context.addInitScript(installAudit);
  await context.route("**/*", route => {
    const request = route.request();
    const url = new URL(request.url());
    if (!["data:", "blob:"].includes(url.protocol) && url.origin !== base.origin) {
      result.externalRequests.push({ url: request.url(), type: request.resourceType() });
      return route.abort("blockedbyclient");
    }
    if (url.pathname.startsWith("/api/") && !/\/batch\/templates\/[ab]\.(csv|xlsx)$/.test(url.pathname)) {
      result.escapedApiRequests.push({ url: request.url(), method: request.method() });
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(timeout);
  page.on("pageerror", error => result.errors.push(error.message));
  page.on("websocket", socket => {
    result.websocketUrls.push(socket.url());
    const url = new URL(socket.url());
    if (url.host !== base.host) result.externalRequests.push({ url: socket.url(), type: "websocket" });
  });
  page.on("response", response => {
    if (response.status() >= 400 && !new URL(response.url()).pathname.startsWith("/api/") &&
        ["script", "stylesheet", "font", "image", "document"].includes(response.request().resourceType())) {
      result.missingResources.push({ url: response.url(), status: response.status() });
    }
  });
  try {
    await execute(page, result);
    result.audit = await page.evaluate(() => window.__handoffBrowserAudit);
    assert.deepEqual(result.errors, [], "No unhandled page errors");
    assert.deepEqual(result.externalRequests, [], "Only local resources may be requested");
    assert.deepEqual(result.escapedApiRequests, [], "Business APIs must be handled by the app Mock");
    assert.deepEqual(result.missingResources, [], "Required local assets must exist");
    assert.deepEqual(result.audit.fetches.filter(entry => entry.status === 501), [], "Mock has no missing API");
    assert.deepEqual(result.audit.violations, [], "CSP must allow required local SDK resources");
    result.passed = true;
  } catch (error) {
    result.failure = error.stack || String(error);
    result.audit = await page.evaluate(() => window.__handoffBrowserAudit).catch(() => null);
    await page.screenshot({ path: resolve(output, `${slug(name)}-failure.png`), fullPage: true }).catch(() => {});
  } finally {
    report.results.push(result);
    console.log(`${result.passed ? "PASS" : "FAIL"} ${name}${result.failure ? `: ${result.failure.split("\n")[0]}` : ""}`);
    await context.close();
    await writeFile(resolve(output, "results.json"), JSON.stringify(report, null, 2));
  }
}

async function openModule(page, path, title, scenario = "success", auth = "authenticated") {
  await page.goto(urlFor(path, scenario, auth), { waitUntil: "domcontentloaded" });
  if (title) await page.getByRole("heading", { name: title, exact: true }).waitFor();
  else await page.locator(".np-app-shell").waitFor();
  await page.waitForFunction(handoffPageReady);
  await page.waitForTimeout(250);
}

async function geometry(page, title) {
  const state = await page.evaluate(() => {
    const roots = [...document.querySelectorAll(".np-module-page")].filter(element =>
      element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0);
    const root = roots[0];
    const heading = root?.querySelector(".np-module-page-title");
    return { roots: roots.length, title: heading?.textContent,
      font: heading ? getComputedStyle(heading).fontFamily : null,
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      editors: document.querySelectorAll("[data-structure-editor]").length };
  });
  assert.equal(state.roots, 1, "Exactly one visible unified module");
  assert.equal(state.title, title);
  assert.equal(state.overflow, false, "No document horizontal overflow");
  assert.ok(state.font.startsWith("Exo"), "Unified title font remains available locally");
  assert.ok(state.editors <= 1, "At most one shared molecular editor");
  return state;
}

async function exerciseKnowledge(page, scenario) {
  await page.getByRole("searchbox", { name: "本地知识库检索词" }).fill("synthetic");
  await page.getByRole("button", { name: "运行检索", exact: true }).click();
  if (scenario === "loading") await page.getByLabel("正在加载本地知识记录").waitFor();
  else if (scenario === "empty") await page.getByRole("heading", { name: "没有匹配的知识记录" }).waitFor();
  else if (scenario === "error") await page.getByRole("heading", { name: "本地知识库暂时不可用" }).waitFor();
  else {
    await page.locator(".ks-result-card").first().waitFor();
    await page.locator(".ks-result-card").first().click();
    await page.locator(".ks-detail-drawer.is-open").waitFor();
  }
}


async function businessRequestEvidence(page, path, status) {
  await page.waitForFunction(({ path, status }) => window.__handoffBrowserAudit.fetches.some(entry =>
    entry.method === "POST" && new URL(entry.url).pathname === path && (status === null || entry.status === status)
  ), { path, status });
  const requests = await page.evaluate(path => window.__handoffBrowserAudit.fetches.filter(entry =>
    entry.method === "POST" && new URL(entry.url).pathname === path
  ).map(entry => ({
    method: entry.method, status: entry.status ?? null,
    pending: entry.responded === undefined, admissionKeyPresent: entry.hasAdmissionKey,
  })), path);
  assert.equal(requests.length, 1, "Exactly one actual business submission for " + path);
  const request = requests[0];
  assert.equal(request.admissionKeyPresent, true, "Enabled admission sends an idempotency key for " + path);
  assert.equal(request.status, status, "The business response matches the selected scenario for " + path);
  assert.equal(request.pending, status === null, "Only the loading business request remains pending for " + path);
  return request;
}

async function exerciseRetrosynthesis(page, scenario) {
  await page.getByRole("textbox", { name: "目标单体 SMILES" }).fill("CCO");
  await page.getByRole("button", { name: "运行反推", exact: true }).click();
  if (scenario === "loading") await page.getByText("正在生成逆合成候选", { exact: true }).waitFor();
  else if (scenario === "empty") await page.getByText("未找到可展示候选", { exact: true }).waitFor();
  else if (scenario === "error") await page.getByText("反推运行失败", { exact: true }).waitFor();
  else await page.getByText(/候选 1/).first().waitFor();
  return businessRequestEvidence(page, "/api/v1/monomer-retrosynthesis",
    scenario === "loading" ? null : scenario === "error" ? 503 : 200);
}

try {
  if (selected("matrix")) {
    for (const [width, height] of viewports) for (const scenario of scenarios) for (const [path, title] of modules) {
      await run(`matrix-${path.slice(1)}-${width}-${scenario}`, { group: "matrix", path, scenario, width, height }, async (page, result) => {
        await openModule(page, path, title, scenario);
        if (path === "/knowledge") await exerciseKnowledge(page, scenario);
        if (path === "/monomer-retrosynthesis") result.retrosynthesis = await exerciseRetrosynthesis(page, scenario);
        result.geometry = await geometry(page, title);
        if (scenario === "success" || ["/knowledge", "/monomer-retrosynthesis"].includes(path)) {
          await page.screenshot({ path: resolve(output, `${slug(result.name)}.png`) });
        }
      });
    }
  }

  if (selected("routes")) {
    for (const path of routePaths) {
      await run(`route-${slug(path)}`, { group: "routes", path }, async (page, result) => {
        await openModule(page, path);
        await page.waitForTimeout(350);
        if (path === "/") {
          await page.getByText("智能体工作区：交付包本地占位", { exact: true }).waitFor();
          const session = await page.evaluate(async () => (await fetch("/api/v1/auth/session")).json());
          assert.equal(session.account_type, "member");
          assert.equal(session.capabilities["openscience.use"], true);
          assert.equal(session.capabilities.dev_gpu_control, false);
          result.localWorkspacePlaceholder = true;
        }
        result.pageText = (await page.locator("main").last().innerText()).slice(0, 1000);
        assert.ok(result.pageText.trim().length > 10, "Route renders its content");
        assert.equal(await page.locator("iframe").count(), 0, "External OpenScience workspace stays a local placeholder");
        const canonical = { "/conditional-generation/polytao": "/polytao-generation", "/database/property-filter": "/database-filter" }[path];
        if (canonical) assert.equal(new URL(page.url()).pathname, canonical, "Legacy route redirects to its current module");
      });
    }
  }

  if (selected("auth")) await run("guest-login-logout", { group: "auth" }, async (page, result) => {
    await openModule(page, "/monomer-retrosynthesis", "单体逆合成反推", "success", "guest");
    const before = await page.evaluate(() => window.__handoffBrowserAudit.fetches.length);
    await page.getByRole("textbox", { name: "目标单体 SMILES" }).fill("CCO");
    await page.getByRole("button", { name: "运行反推", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "登录工作空间" });
    await dialog.waitFor();
    const guestRequests = await page.evaluate(before => window.__handoffBrowserAudit.fetches.slice(before), before);
    assert.deepEqual(guestRequests.filter(entry => !new URL(entry.url).pathname.includes("/auth/")), [], "Guest action does not call business APIs");
    await dialog.getByLabel("用户名", { exact: true }).fill("handoff");
    await dialog.getByLabel("密码", { exact: true }).fill("demo-password");
    await dialog.getByRole("button", { name: "登录", exact: true }).click();
    const startBlank = page.getByRole("button", { name: "从空白开始", exact: true });
    await Promise.race([startBlank.waitFor().then(() => startBlank.click()), page.getByRole("button", { name: "账号菜单：handoff", exact: true }).waitFor()]);
    await page.getByRole("textbox", { name: "目标单体 SMILES" }).fill("CCO");
    await page.getByRole("button", { name: "运行反推", exact: true }).click();
    await page.getByText(/候选 1/).first().waitFor();
    result.authenticatedBusinessRequest = await businessRequestEvidence(page, "/api/v1/monomer-retrosynthesis", 200);
    await page.getByRole("button", { name: "关闭单体反推结果", exact: true }).click();
    await page.getByRole("button", { name: "账号菜单：handoff", exact: true }).click();
    await page.getByRole("menuitem", { name: "退出登录", exact: true }).click();
    await page.getByRole("button", { name: "游客账号菜单", exact: true }).waitFor();
    const session = await page.evaluate(async () => (await fetch("/api/v1/auth/session")).json());
    assert.equal(session.authenticated, false);
    result.guestBusinessRequests = 0;
    result.loginLogout = true;
  });

  if (selected("editor")) await run("real-local-structure-editor", { group: "editor" }, async (page, result) => {
    await openModule(page, "/structure-workbench", "结构工作台");
    const editor = page.locator('[data-structure-editor][data-editor-status="ready"]');
    await editor.waitFor({ timeout: 60000 });
    result.engine = await editor.getAttribute("data-editor-engine");
    assert.equal(result.engine, "react", "Current native Ketcher engine is preserved");
    await page.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" }).fill("CCO");
    await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]')?.disabled);
    await page.getByRole("button", { name: "生成SMILES", exact: true }).click();
    await page.getByText("SMILES 与画板布局已同步。", { exact: true }).waitFor();
    assert.equal(await page.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" }).inputValue(), "CCO");
    await page.locator('[data-workbench-tool="3d"]').click();
    result.structure3DRequest = await businessRequestEvidence(page, "/api/v1/structure/3d", 200);
    const visible3D = page.locator('.np-sw-editor__layer--3d[aria-hidden="false"]');
    await visible3D.locator("canvas").first().waitFor({ state: "visible" });
    assert.equal(await visible3D.locator(".np-sw-preview-3d__status--error").count(), 0, "Real local 3D has no error state");
    const bounds = await visible3D.locator("canvas").first().boundingBox();
    assert.ok(bounds && bounds.width > 0 && bounds.height > 0, "Real 3D viewer has a visible canvas");
    await page.screenshot({ path: resolve(output, "editor-local-3d.png") });
    result.nativeEditorAnd3D = true;
  });

  if (selected("batch")) await run("batch-template-import-results", { group: "batch" }, async (page, result) => {
    await openModule(page, "/monomer-polymerization", "单体正向聚合");
    await page.getByRole("tab", { name: "批量聚合", exact: true }).click();
    const template = page.getByRole("link", { name: "CSV 模板", exact: true }).first();
    const downloaded = page.waitForEvent("download");
    await template.click();
    const download = await downloaded;
    assert.equal(await download.failure(), null);
    const templateData = await readFile(await download.path(), "utf8");
    assert.match(templateData, /smiles/i, "Downloaded CSV is a usable input template");
    result.template = download.suggestedFilename();
    const excelDownloaded = page.waitForEvent("download");
    await page.getByRole("link", { name: "Excel 模板", exact: true }).first().click();
    const excelDownload = await excelDownloaded;
    assert.equal(await excelDownload.failure(), null);
    const excelMembers = inspectZip(await readFile(await excelDownload.path()));
    assert.ok(excelMembers.some(file => file.name === "[Content_Types].xml") &&
      excelMembers.some(file => file.name.startsWith("xl/worksheets/")), "Excel template is a real workbook");
    result.excelTemplate = excelDownload.suggestedFilename();
    await page.getByLabel("上传单体表 A", { exact: true }).setInputFiles({ name: "synthetic-a.csv", mimeType: "text/csv", buffer: Buffer.from("id,name,smiles\nSYN-A,Synthetic diamine,NCCN\n") });
    await page.getByLabel("上传单体表 B", { exact: true }).setInputFiles({ name: "synthetic-b.csv", mimeType: "text/csv", buffer: Buffer.from("id,name,smiles\nSYN-B,Synthetic acid,O=C(O)CC(=O)O\n") });
    await page.getByRole("button", { name: "上传并预检", exact: true }).click();
    await page.getByRole("region", { name: "预检结果", exact: true }).waitFor();
    await page.getByRole("button", { name: "开始批量聚合", exact: true }).click();
    result.batchSubmission = await businessRequestEvidence(page, "/api/v1/monomer-polymerization/batch/jobs", 201);
    const artifact = page.getByRole("button", { name: "下载 CSV 结果包", exact: true });
    await artifact.waitFor();
    const resultDownload = page.waitForEvent("download");
    await artifact.click();
    const resultFile = await resultDownload;
    assert.equal(await resultFile.failure(), null);
    const bytes = await readFile(await resultFile.path());
    assert.ok(bytes.length > 20, "Batch artifact is not empty");
    const members = inspectZip(bytes);
    assert.ok(members.some(file => file.name.endsWith(".csv") && file.bytes > 0), "Results archive contains usable CSV members");
    result.artifact = { filename: resultFile.suggestedFilename(), bytes: bytes.length, members };
    await page.screenshot({ path: resolve(output, "batch-completed.png"), fullPage: true });
  });

  if (selected("streams")) for (const [path, title, module] of [
    ["/monomer-md-simulation", "单体 MD 模拟", "md"], ["/monomer-dft", "单体 DFT", "dft"],
  ]) await run(`stable-task-stream-${module}`, { group: "streams", path }, async (page, result) => {
    await openModule(page, path, title);
    await page.waitForFunction(module => window.__handoffBrowserAudit.fetches.some(entry =>
      entry.url.includes(`/task-events?module=${module}`) && entry.contentType?.includes("text/event-stream")), module);
    await page.waitForTimeout(750);
    const sample = () => page.evaluate(module => window.__handoffBrowserAudit.fetches.filter(entry => entry.url.includes(`/task-events?module=${module}`)).length, module);
    const before = await sample();
    await page.waitForTimeout(4000);
    assert.equal(await sample(), before, "A healthy Mock SSE stream must not reconnect repeatedly");
    assert.equal(await page.getByText(/状态通知连接中断|状态通知暂不可用/).count(), 0);
    result.streamConnections = before;
    result.stableIntervalMs = 4000;
    if (module === "dft") {
      await page.locator("#monomer-dft-main-tab-tasks").click();
      const running = page.locator(".np-dft-history-list article").filter({ has: page.locator(".is-running") }).first();
      await running.waitFor();
      const jobId = await running.locator("code").innerText();
      await running.locator(".np-dft-history-select").click();
      const current = page.locator(".np-dft-selected-job");
      await current.getByRole("button", { name: "取消任务", exact: true }).click();
      await current.locator(".is-cancelled").waitFor();
      page.once("dialog", dialog => dialog.accept());
      await current.getByRole("button", { name: "删除记录", exact: true }).click();
      await page.waitForFunction(jobId => window.__handoffBrowserAudit.fetches.some(entry =>
        entry.method === "DELETE" && entry.url.endsWith(`/jobs/${jobId}`) && entry.status === 204), jobId);
      await page.locator("#monomer-dft-main-tab-tasks").click();
      await page.locator(".np-dft-history-list").waitFor();
      await page.locator(".np-dft-history-list article").filter({ hasText: jobId }).waitFor({ state: "detached" });
      await page.getByRole("button", { name: "刷新任务历史", exact: true }).click();
      await page.waitForTimeout(250);
      assert.equal(await page.locator(".np-dft-history-list article").filter({ hasText: jobId }).count(), 0,
        "Deleting a synthetic task remains effective after history refresh");
      result.cancelAndDelete = jobId;
    }
  });
} finally {
  await browser.close();
  report.finishedAt = new Date().toISOString();
  report.total = report.results.length;
  report.failed = report.results.filter(result => !result.passed).length;
  report.countMismatch = report.total !== expectedTotal;
  report.passed = report.total > 0 && !report.countMismatch && report.failed === 0;
  await writeFile(resolve(output, "results.json"), JSON.stringify(report, null, 2));
  console.log(`Handoff verification: ${report.total - report.failed}/${report.total} passed; ${resolve(output, "results.json")}`);
  if (!report.passed) process.exitCode = 1;
}
