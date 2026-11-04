// Probe a served production build with both account display flags disabled.
// Usage: node scripts/ci/verify_frontend_account_ui.mjs http://127.0.0.1:5299
// ACCOUNT_UI_BASE_URL may replace the argument. ACCOUNT_UI_ARTIFACT_DIR and
// ACCOUNT_UI_CHROMIUM_PATH configure artifacts and the Chromium executable.
// No API request or iframe request is ever forwarded to a running service.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "../../frontend/node_modules/playwright/index.mjs";

if (process.argv.includes("--help")) {
  console.log("Usage: node scripts/ci/verify_frontend_account_ui.mjs <base-url>\nEnv: ACCOUNT_UI_BASE_URL, ACCOUNT_UI_ARTIFACT_DIR, ACCOUNT_UI_CHROMIUM_PATH");
  process.exit(0);
}
assert.ok(process.argv[2] || process.env.ACCOUNT_UI_BASE_URL, "A served production build URL is required; use --help for options");
const target = new URL(process.argv[2] || process.env.ACCOUNT_UI_BASE_URL);
assert.ok(["http:", "https:"].includes(target.protocol) && !target.username && !target.password,
  "The target must be an HTTP(S) URL without credentials");
assert.equal(target.port === "9011", false, "This probe must never target the OpenScience service on 9011");
assert.equal(target.pathname + target.search + target.hash, "/", "Supply an origin URL; the probe selects its own paths");
const base = target.origin;
const output = process.env.ACCOUNT_UI_ARTIFACT_DIR
  ? resolve(process.env.ACCOUNT_UI_ARTIFACT_DIR)
  : await mkdtemp(join(tmpdir(), "nexpoly-account-ui-"));
await mkdir(output, { recursive: true });

const guest = { authenticated: false, user: null, session_id: null, csrf_token: null, capabilities: {} };
function account(type) {
  return { authenticated: true, account_type: type,
    user: { id: `fixture-account-${type}`, username: `account-probe-${type}`, must_change_password: false },
    session_id: `fixture-session-${type}`, csrf_token: `fixture-csrf-${type}`,
    capabilities: { "openscience.use": type === "member" } };
}
const workspace = `<!doctype html><meta charset="utf-8"><title>Synthetic workspace</title>
<p data-testid="account-fixture-workspace">Synthetic read-only workspace</p><script>
const namespace = "openscience.zhijupoly", version = 1, parentOrigin = ${JSON.stringify(base)};
function emit(type, body) { parent.postMessage({ namespace, version, type, ...body }, parentOrigin); }
function sessions() { emit("general.sessions.snapshot", { sessions: [{ id: "fixture-history", title: "合成历史会话", updatedAt: 100 }], activeSessionID: "fixture-history" }); }
addEventListener("message", event => {
  if (event.source !== parent || event.origin !== parentOrigin || event.data?.namespace !== namespace) return;
  if (event.data.type === "general.sessions.request") sessions();
  if (event.data.type === "projects.request") emit("projects.snapshot", { projects: [], activeDirectory: null });
});
sessions(); emit("projects.snapshot", { projects: [], activeDirectory: null });
</script>`;
const report = { target: base, expectedBuildFlags: { VITE_SUBSCRIPTION_ENABLED: false, VITE_REGISTRATION_ENABLED: false },
  syntheticFixtures: true, realApiRequests: 0, realWrites: 0, realIframeRequests: 0, cases: [] };
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"],
  executablePath: process.env.ACCOUNT_UI_CHROMIUM_PATH || undefined });

async function scenario(name, initialSession, run) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 },
    reducedMotion: "reduce", serviceWorkers: "block" });
  let session = initialSession;
  const network = { api: [], syntheticMutations: [], billing: [], unexpectedWrites: [],
    unexpectedReads: [], externalBlocked: [], fixtureFrames: [], scripts: [], webSockets: [] };
  const pageErrors = [], routeErrors = [];
  await context.routeWebSocket("**/*", socket => { network.webSockets.push(socket.url()); socket.close(); });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    const record = `${method} ${url.pathname}`;
    const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json",
      headers: { "Cache-Control": "no-store" }, body: JSON.stringify(value) });
    try {
      // Treat even cross-origin/misconfigured API URLs as fixtures. Nothing in
      // this branch can reach a backend, including unexpected billing calls.
      if (url.pathname.startsWith("/api/")) {
        network.api.push(record);
        if (/\/billing(?:\/|$)/.test(url.pathname)) {
          network.billing.push(record);
          return json({ detail: "Billing must stay unreachable in this build" }, 503);
        }
        if (method === "GET" && url.pathname === "/api/v1/auth/session") return json(session);
        if (method === "POST" && url.pathname === "/api/v1/auth/login") {
          assert.deepEqual(request.postDataJSON(), { username: "account-probe-member", password: "synthetic-password-only" });
          network.syntheticMutations.push(record); session = account("member"); return json(session);
        }
        if (method === "POST" && url.pathname === "/api/v1/auth/logout") {
          assert.equal(request.headers()["x-session-context"], session.session_id);
          assert.equal(request.headers()["x-csrf-token"], session.csrf_token);
          network.syntheticMutations.push(record); session = guest;
          return route.fulfill({ status: 204, headers: { "Cache-Control": "no-store" } });
        }
        if (!["GET", "HEAD"].includes(method)) {
          network.unexpectedWrites.push(record); return route.abort();
        }
        if (url.pathname.startsWith("/api/v1/usage/")) {
          assert.equal(request.headers()["x-session-context"], session.session_id);
          if (url.pathname.endsWith("/entitlements")) return json({ enabled: false });
          if (url.pathname.endsWith("/catalog")) return json({ enabled: false, mode: "statistics_only", modules: [], module_groups: [], metrics: [] });
          if (url.pathname.endsWith("/operations")) return json({ items: [], offset: 0, limit: 25, has_more: false, module_groups: [] });
          if (url.pathname.endsWith("/activity")) return json({ mode: "activity_only", items: [], offset: 0, limit: 25, has_more: false, modules: [], module_groups: [], warnings: [] });
        }
        network.unexpectedReads.push(record);
        return json({ detail: "Unexpected API: synthetic fixture only" }, 503);
      }
      if (!["GET", "HEAD"].includes(method)) {
        network.unexpectedWrites.push(record); return route.abort();
      }
      // This runs before the same-origin branch so a configured 9011 frame,
      // including all of its redirects, is fulfilled locally without a socket.
      if (request.isNavigationRequest() && request.frame().parentFrame()) {
        network.fixtureFrames.push(url.origin + url.pathname);
        return route.fulfill({ contentType: "text/html", body: workspace });
      }
      if (url.origin !== base || url.port === "9011") {
        network.externalBlocked.push(url.origin + url.pathname); return route.abort();
      }
      if (request.resourceType() === "script") network.scripts.push(url.pathname);
      assert.ok(!url.pathname.startsWith("/src/") && url.pathname !== "/@vite/client",
        "The probe requires a production build, not the Vite development server");
      // Only main documents and static resources can be fetched upstream.
      if (request.isNavigationRequest() || /^(?:script|stylesheet|image|font|manifest|media)$/.test(request.resourceType())
        || /^\/(?:assets|vendor)\//.test(url.pathname)) return route.continue();
      network.unexpectedReads.push(record); return route.abort();
    } catch (error) {
      routeErrors.push(error.message); await route.abort().catch(() => {});
    }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on("pageerror", error => pageErrors.push(error.message));
  const trigger = () => page.locator(".np-sidebar-desktop .np-account-menu__trigger");
  async function settle() {
    await trigger().waitFor({ state: "visible" });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    // A synthetic cross-origin workspace need not share the main document's
    // network-idle lifecycle. Assert the visible shell and its own transition.
    await trigger().waitFor({ state: "visible" });
    await page.locator('[data-module-content][data-module-phase="idle"]:not([inert])').first().waitFor();
  }
  async function menu() {
    await trigger().click();
    const menu = page.getByRole("menu", { name: "账号操作", exact: true });
    await menu.waitFor({ state: "visible" }); return menu;
  }
  async function hiddenFeatures() {
    assert.equal(await page.getByText("注册账号", { exact: true }).count(), 0, "Registration must have no rendered entry");
    assert.equal(await page.getByText("会员订阅", { exact: true }).count(), 0, "Subscription must have no rendered entry");
    assert.equal(await page.getByText(/开通会员/).count(), 0, "Hidden subscriptions must not be promoted");
    assert.equal(await page.locator(".np-subscription-page, .np-simulated-payment").count(), 0,
      "Neither the subscription page nor the public payment page may mount");
    assert.deepEqual(network.billing, [], "Hidden subscriptions must issue no billing API requests");
    assert.ok(!network.scripts.some(path => /(?:mountSimulationPayment|SimulatedPaymentPage)[-.]/.test(path)),
      "The public payment entry must not be loaded");
  }
  try {
    await run({ page, menu, settle, hiddenFeatures, network });
    await settle(); await hiddenFeatures();
    assert.deepEqual(routeErrors, [], "Request interception failed");
    assert.deepEqual(pageErrors, [], "The production build raised a browser error");
    assert.deepEqual(network.unexpectedWrites, [], "Unexpected writes were blocked");
    assert.deepEqual(network.unexpectedReads, [], "Unexpected service reads were blocked");
    assert.deepEqual(network.webSockets, [], "The fixture must not open service sockets");
    assert.ok(network.scripts.some(path => /^\/assets\/.*\.js$/.test(path)), "No production JavaScript was exercised");
    await page.screenshot({ path: join(output, `${name}.png`), fullPage: true });
    report.cases.push({ name, status: "passed", ...network });
  } catch (error) {
    await page.screenshot({ path: join(output, `${name}-failure.png`), fullPage: true }).catch(() => {});
    await writeFile(join(output, `${name}-failure.json`), JSON.stringify({ error: error.message,
      url: page.url(), pageErrors, routeErrors, network, body: await page.locator("body").innerText().catch(() => "") }, null, 2));
    throw new Error(`${name}: ${error.message}`, { cause: error });
  } finally { await context.close(); }
}

try {
  await scenario("guest-login", guest, async ({ page, menu, settle, hiddenFeatures, network }) => {
    await page.goto(base); await settle();
    await (await menu()).getByRole("menuitem", { name: "登录", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "登录工作空间", exact: true });
    await dialog.waitFor(); await hiddenFeatures();
    await dialog.getByLabel("用户名", { exact: true }).fill("account-probe-member");
    await dialog.getByLabel("密码", { exact: true }).fill("synthetic-password-only");
    await dialog.getByRole("button", { name: "登录", exact: true }).click();
    await page.locator(".np-sidebar-desktop").getByRole("button", { name: "账号菜单：account-probe-member", exact: true }).waitFor();
    await settle();
    assert.deepEqual(network.syntheticMutations, ["POST /api/v1/auth/login"]);
    assert.equal(await dialog.count(), 0, "Synthetic login must finish and close the login dialog");
    assert.equal(await page.locator(".np-sidebar-desktop").getByRole("button", { name: "账号菜单：account-probe-member", exact: true }).isVisible(), true);
  });

  for (const type of ["member", "free"]) {
    await scenario(`${type}-account`, account(type), async ({ page, menu, settle, hiddenFeatures, network }) => {
      await page.goto(base); await settle();
      let actions = await menu(); await hiddenFeatures();
      for (const name of ["我的用量", "修改密码", "退出登录"]) {
        assert.equal(await actions.getByRole("menuitem", { name, exact: true }).isEnabled(), true, `${type}: ${name} remains available`);
      }
      await actions.getByRole("menuitem", { name: "我的用量", exact: true }).click();
      await page.getByRole("tablist", { name: "用量视图", exact: true }).waitFor();
      await settle(); assert.equal(new URL(page.url()).pathname, "/account/usage");
      await page.getByText("分类次数尚未启用。", { exact: false }).waitFor();
      await page.getByRole("tab", { name: "操作记录", exact: true }).click();
      await page.getByRole("tab", { name: "资源观测", exact: true }).click();
      await page.getByRole("heading", { name: "用量统计尚未启用", exact: true }).waitFor();
      assert.ok(network.api.includes("GET /api/v1/usage/entitlements"));
      assert.ok(network.api.includes("GET /api/v1/usage/catalog"));
      const beforeEvent = page.url();
      await page.evaluate(() => window.dispatchEvent(new Event("nexpoly:open-subscription")));
      await settle(); assert.equal(page.url(), beforeEvent, "A hidden subscription event must preserve the current page");
      await hiddenFeatures();
      if (type === "free") {
        await page.evaluate(() => window.dispatchEvent(new Event("nexpoly:membership-required")));
        const permission = page.getByRole("dialog", { name: "功能权限说明", exact: true });
        await permission.getByRole("heading", { name: "当前账号暂无此功能权限", exact: true }).waitFor();
        await hiddenFeatures();
        await permission.getByRole("button", { name: "关闭说明", exact: true }).last().click();
      }
      actions = await menu();
      await actions.getByRole("menuitem", { name: "修改密码", exact: true }).click();
      await page.getByRole("heading", { name: "修改密码", exact: true }).waitFor();
      for (const label of ["当前密码", "新密码", "确认新密码"]) assert.equal(await page.getByLabel(label, { exact: true }).isVisible(), true);
      await page.getByRole("button", { name: "返回工作空间", exact: true }).click(); await settle();
      await (await menu()).getByRole("menuitem", { name: "退出登录", exact: true }).click();
      await page.locator(".np-sidebar-desktop").getByRole("button", { name: "游客账号菜单", exact: true }).waitFor();
      await settle();
      assert.deepEqual(network.syntheticMutations, ["POST /api/v1/auth/logout"]);
      assert.equal(await (await menu()).getByRole("menuitem", { name: "登录", exact: true }).isVisible(), true);
    });
  }

  const blockedPaths = ["/account/subscription", "/account/subscription/?old=order#private",
    "/billing/simulate-pay", "/billing/simulate-pay/", "/billing/simulate-pay#token=synthetic-token",
    "/billing/simulate-pay///?token=synthetic-token#token=synthetic-token"];
  await scenario("disabled-deep-links", account("member"), async ({ page, settle, hiddenFeatures, network }) => {
    for (const path of blockedPaths) {
      await page.goto(base + path); await settle(); await hiddenFeatures();
      assert.equal(new URL(page.url()).pathname + new URL(page.url()).search + new URL(page.url()).hash, "/",
        `Disabled entry must normalize to home and discard checkout data: ${path}`);
      await page.getByRole("heading", { name: "对话", exact: true }).waitFor();
    }
    await page.evaluate(() => {
      history.pushState({}, "", "/account/subscription/?old=order#private");
      dispatchEvent(new PopStateEvent("popstate"));
    });
    await settle(); await hiddenFeatures();
    assert.equal(page.url(), base + "/", "History navigation must also normalize a disabled subscription");
    assert.deepEqual(network.syntheticMutations, []);
  });
  await writeFile(join(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ status: "passed", cases: report.cases.length, realApiRequests: 0,
    realWrites: 0, realIframeRequests: 0, output }));
} finally { await browser.close(); }
