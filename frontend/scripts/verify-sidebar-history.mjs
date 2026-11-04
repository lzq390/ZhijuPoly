// Browser regression with synthetic identity/workspace data; no API reaches a backend.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const target = new URL(process.env.SIDEBAR_BASE_URL || "http://127.0.0.1:5297");
assert.ok(["http:", "https:"].includes(target.protocol) && !target.username && !target.password);
const base = target.origin;
const output = resolve(process.env.SIDEBAR_ARTIFACT_DIR || "/tmp/nexpoly-sidebar-history");
await mkdir(output, { recursive: true });
const identity = {
  authenticated: true, account_type: "member",
  user: { id: "fixture-sidebar-owner", username: "侧栏测试", must_change_password: false },
  session_id: "fixture-sidebar-session", csrf_token: "fixture-only-csrf",
  capabilities: { "openscience.use": true }
};
const workspace = `<!doctype html><html lang="zh"><meta charset="utf-8"><title>合成工作空间</title>
<body><p data-testid="fixture-view">session:ses_recent</p><script>
const namespace = "openscience.zhijupoly", version = 1, parentOrigin = ${JSON.stringify(base)};
const sessions = [
  { id: "ses_recent", title: "侧栏回归：近期研究", updatedAt: 200 },
  { id: "ses_history", title: "侧栏回归：历史记录", updatedAt: 100 }
];
const directory = "/fixture/sidebar-project";
const projects = [{ directory, name: "侧栏回归项目", displayPath: "~/fixture/sidebar-project", updatedAt: 100, favorite: false }];
let activeSessionID = "ses_recent", activeDirectory = null;
window.__sidebarFixtureCommands = [];
function emit(type, body) { parent.postMessage({ namespace, version, type, ...body }, parentOrigin); }
function sendSessions() { emit("general.sessions.snapshot", { sessions, activeSessionID }); }
function sendProjects() { emit("projects.snapshot", { projects, activeDirectory }); }
addEventListener("message", event => {
  if (event.source !== parent || event.origin !== parentOrigin || event.data?.namespace !== namespace) return;
  const message = event.data;
  window.__sidebarFixtureCommands.push(message);
  if (message.type === "projects.request") sendProjects();
  if (message.type === "general.sessions.request") sendSessions();
  if (message.type === "project.open" && message.directory === directory) {
    activeDirectory = directory;
    document.querySelector("[data-testid=fixture-view]").textContent = "project:" + directory;
    sendProjects();
  }
  if (message.type === "general.session.open" && sessions.some(session => session.id === message.sessionID)) {
    activeDirectory = null;
    activeSessionID = message.sessionID;
    document.querySelector("[data-testid=fixture-view]").textContent = "session:" + activeSessionID;
    sendProjects(); sendSessions();
  }
});
sendProjects(); sendSessions();
</script></body></html>`;

const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"],
  executablePath: process.env.SIDEBAR_CHROMIUM_PATH || undefined });
const report = { fixture: true, backendRequests: 0, cases: [] };
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
    const blockedWrites = [], errors = [], reads = [];
    await context.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      if (!["GET", "HEAD"].includes(request.method())) {
        blockedWrites.push(request.method() + " " + url.pathname);
        return route.abort();
      }
      if (request.isNavigationRequest() && request.frame().parentFrame()) {
        return route.fulfill({ contentType: "text/html", body: workspace });
      }
      if (url.origin !== base) return route.abort();
      if (url.pathname.startsWith("/api/")) {
        reads.push(url.pathname);
        return route.fulfill({ status: url.pathname === "/api/v1/auth/session" ? 200 : 503,
          contentType: "application/json", headers: { "Cache-Control": "no-store" },
          body: JSON.stringify(url.pathname === "/api/v1/auth/session" ? identity : { detail: "Synthetic read-only fixture" }) });
      }
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on("pageerror", error => errors.push(error.message));
    async function sidebar() {
      if (viewport.width < 1024) {
        if (!(await page.getByRole("dialog", { name: "平台导航" }).isVisible())) {
          await page.getByRole("button", { name: "打开导航", exact: true }).click();
        }
        return page.getByRole("dialog", { name: "平台导航" });
      }
      return page.locator(".np-sidebar-desktop");
    }
    async function settled(module) {
      await page.locator(`[data-module-content="${module}"][data-module-phase="idle"]:not([inert])`).waitFor();
    }
    async function verifyHistory(selected) {
      const nav = await sidebar();
      const recent = nav.getByRole("button", { name: "侧栏回归：近期研究", exact: true });
      const historical = nav.getByRole("button", { name: "侧栏回归：历史记录", exact: true });
      await historical.waitFor({ state: "visible" });
      assert.equal(await recent.isVisible(), true);
      assert.equal(await nav.getByRole("button", { name: "新建对话", exact: true }).isEnabled(), true);
      assert.equal(await nav.locator(".np-sidebar-section__count").last().textContent(), "2");
      assert.equal(await historical.getAttribute("aria-current"), selected ? "page" : null);
      assert.equal(await recent.getAttribute("aria-current"), null);
      const search = nav.getByRole("searchbox", { name: "搜索会话", exact: true });
      await search.fill("历史记录");
      assert.equal(await recent.count(), 0);
      await historical.waitFor({ state: "visible" });
      await search.fill("");
      return nav;
    }
    try {
      await page.goto(base);
      let nav = await sidebar();
      await nav.getByRole("button", { name: "侧栏回归：近期研究", exact: true }).waitFor();
      await page.evaluate(() => { window.__sidebarOriginalFrame = document.querySelector('iframe[title="智聚万物智能体工作台"]'); });
      const group = nav.getByRole("button", { name: "材料设计 Build", exact: true });
      if (await group.getAttribute("aria-expanded") === "false") await group.click();
      await nav.locator('[data-module-id="monomerMdSimulation"]').click();
      await settled("monomerMdSimulation");
      await page.locator(".np-monomer-md-simulation").waitFor({ state: "visible" });
      nav = await verifyHistory(false);
      await page.screenshot({ path: resolve(output, `md-${viewport.width}.png`), fullPage: true });
      await nav.getByRole("button", { name: "侧栏回归：历史记录", exact: true }).click();
      await settled("home");
      await page.frameLocator('iframe[title="智聚万物智能体工作台"]').getByTestId("fixture-view").filter({ hasText: "session:ses_history" }).waitFor();
      nav = await verifyHistory(true);
      const expandProjects = nav.getByRole("button", { name: "展开项目", exact: true });
      if (await expandProjects.count()) await expandProjects.click();
      await nav.locator('[data-project-directory="/fixture/sidebar-project"]').click();
      await page.frameLocator('iframe[title="智聚万物智能体工作台"]').getByTestId("fixture-view").filter({ hasText: "project:/fixture/sidebar-project" }).waitFor();
      nav = await verifyHistory(false);
      await nav.getByRole("button", { name: "侧栏回归：历史记录", exact: true }).click();
      await page.frameLocator('iframe[title="智聚万物智能体工作台"]').getByTestId("fixture-view").filter({ hasText: "session:ses_history" }).waitFor();
      await verifyHistory(true);
      assert.equal(await page.evaluate(() => window.__sidebarOriginalFrame === document.querySelector('iframe[title="智聚万物智能体工作台"]')), true);
      const frame = page.frames().find(value => value.parentFrame());
      const commands = await frame.evaluate(() => window.__sidebarFixtureCommands);
      assert.equal(commands.filter(message => message.type === "general.session.open" && message.sessionID === "ses_history").length, 2);
      assert.deepEqual(blockedWrites, []);
      assert.deepEqual(errors, []);
      report.cases.push({ viewport, mdHistoryVisible: true, projectHistoryVisible: true,
        correctHistoryOpened: true, iframePreserved: true, interceptedReads: [...new Set(reads)].sort(), commands });
    } catch (error) {
      await page.screenshot({ path: resolve(output, `failure-${viewport.width}.png`), fullPage: true });
      await writeFile(resolve(output, `failure-${viewport.width}.json`), JSON.stringify({
        error: error.message, errors, blockedWrites, html: await page.locator("body").innerHTML()
      }, null, 2));
      throw error;
    } finally {
      await context.close();
    }
  }
  await writeFile(resolve(output, "results.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ status: "passed", cases: report.cases.length, backendRequests: 0, output }));
} finally {
  await browser.close();
}
