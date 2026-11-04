// Fixture-only browser acceptance. No payment provider, account, or real money is contacted.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = "http://127.0.0.1:5298";
const output = resolve(process.env.PAGE_ARTIFACT_DIR || "/tmp/nexpoly-subscription");
await mkdir(output, { recursive: true });
const server = spawn(process.execPath, ["node_modules/vite/bin/vite.js", ...(process.env.PAGE_SERVER_MODE === "dev" ? [] : ["preview"]), "--host", "127.0.0.1", "--port", "5298", "--strictPort", "--config", "vite.config.ts"], { stdio: "ignore" });
let browser, lastPage;
const report = { scope: "browser UI with synthetic payment responses; no real transaction", checks: [] };
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error("Preview server exited");
    try { if ((await fetch(base)).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready);
  browser = await chromium.launch({ headless: true, executablePath: process.env.PAGE_CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
  for (const [viewport, provider, simulation] of [[{ width: 1440, height: 1000 }, "wechat", false], [{ width: 390, height: 844 }, "alipay", false], [{ width: 1440, height: 1000 }, "wechat", true], [{ width: 390, height: 844 }, "alipay", true]]) {
    const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
    const token = `${"a".repeat(32)}.${"b".repeat(43)}`;
    const state = { member: false, paid: false, legacy: false, created: 0, confirmed: 0, order: null, calls: [] };
    await context.route("**/*", async route => {
      const request = route.request(); const url = new URL(request.url());
      if (url.origin !== base) return route.abort();
      if (!url.pathname.startsWith("/api/")) return route.continue();
      state.calls.push(url.pathname);
      const json = value => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
      if (url.pathname.endsWith("/auth/session")) return json({ authenticated: true, user: { id: "fixture", username: "测试用户", must_change_password: false }, account_type: state.member ? "member" : "free", session_id: "same-session", csrf_token: "fixture-csrf", capabilities: {} });
      if (url.pathname.endsWith("/billing/plans")) return json({ enabled: true, simulation, currency: "CNY", monthly_price_fen: 9900, period: "month", providers: ["wechat", "alipay"], registration_enabled: true });
      if (url.pathname.endsWith("/billing/subscription")) return json({ account_type: state.member ? "member" : "free", source: state.legacy ? "legacy" : state.member ? "subscription" : "none", expires_at: state.member && !state.legacy ? "2027-01-01T00:00:00Z" : null, server_time: new Date().toISOString() });
      if (url.pathname.endsWith("/billing/orders") && request.method() === "POST") {
        state.created++; assert.ok(request.headers()["idempotency-key"]); assert.equal(request.postDataJSON().provider, provider);
        state.order = { id: "fixture-order", provider, amount_fen: 9900, currency: "CNY", status: "pending", qr_code: simulation ? null : provider === "wechat" ? "weixin://fixture-only" : "https://example.invalid/fixture-only", simulation_checkout_token: simulation ? token : null, expires_at: new Date(Date.now() + 3600000).toISOString(), created_at: new Date().toISOString(), paid_at: null, access_ends_at: null };
        return json(state.order);
      }
      if (url.pathname.endsWith("/billing/orders")) return json({ items: state.order ? [state.order] : [] });
      if (url.pathname.endsWith("/billing/orders/fixture-order")) {
        if (state.paid) { state.member = true; state.order = { ...state.order, status: "paid", paid_at: new Date().toISOString(), access_ends_at: "2027-01-01T00:00:00Z" }; }
        return json(state.order);
      }
      throw new Error(`Unexpected service call: ${url.pathname}`);
    });
    const page = await context.newPage(); lastPage = page;
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/account/subscription`);
    await page.getByRole("button", { name: "开通一个月", exact: true }).waitFor();
    await page.evaluate(() => sessionStorage.setItem("nexpoly.fixture.draft", "CCO"));
    await page.getByRole("radio", { name: `${provider === "wechat" ? "微信支付" : "支付宝"}${simulation ? "（开发模拟）" : ""}`, exact: true }).check();
    await page.getByRole("button", { name: "开通一个月", exact: true }).click();
    const qr = page.getByAltText(simulation ? `${provider === "wechat" ? "微信" : "支付宝"}模拟扫码二维码` : provider === "wechat" ? "微信支付二维码" : "支付宝二维码");
    await qr.waitFor(); assert.ok((await qr.getAttribute("src")).startsWith("data:image/png;base64,"));
    await qr.scrollIntoViewIfNeeded();
    await page.screenshot({ path: resolve(output, `subscription-${viewport.width}${simulation ? "-simulation" : ""}.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    if (simulation) {
      assert.equal(await page.getByRole("button", { name: "模拟支付成功（不会扣款）" }).count(), 0);
      const link = await page.getByRole("link", { name: "在新窗口打开模拟确认页" }).getAttribute("href");
      assert.equal(link, `${base}/billing/simulate-pay#token=${token}`);
      const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: "reduce" });
      const phoneCalls = [];
      await phone.route("**/*", async route => {
        const request = route.request(), url = new URL(request.url());
        assert.equal(url.origin, base, "Phone must not load third-party resources");
        assert.equal(url.hash, ""); assert.ok(!request.url().includes(token));
        if (!url.pathname.startsWith("/api/")) return route.continue();
        phoneCalls.push(url.pathname);
        assert.equal(request.method(), "POST"); assert.equal(request.headers().cookie, undefined); assert.equal(request.headers().referer, undefined);
        assert.deepEqual(request.postDataJSON(), { token });
        if (url.pathname.endsWith("/simulation/confirm")) {
          state.confirmed++; state.member = true;
          state.order = { ...state.order, status: "paid", paid_at: new Date().toISOString(), access_ends_at: "2027-01-01T00:00:00Z" };
        } else assert.ok(url.pathname.endsWith("/simulation/checkout"), "Phone must not access sessions or private workspace APIs");
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ simulation: true, provider, amount_fen: 9900, currency: "CNY", period: "month", status: state.order.status, order_suffix: "abc123", expires_at: state.order.expires_at, server_time: new Date().toISOString(), access_ends_at: state.order.access_ends_at }) });
      });
      const mobile = await phone.newPage(); mobile.on("pageerror", error => errors.push(error.message));
      await mobile.goto(link); await mobile.getByRole("button", { name: "确认模拟支付（不会扣款）" }).waitFor();
      await mobile.reload(); await mobile.getByRole("button", { name: "确认模拟支付（不会扣款）" }).waitFor();
      assert.equal(state.confirmed, 0); assert.equal(state.member, false); assert.equal(state.order.status, "pending");
      assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await mobile.screenshot({ path: resolve(output, `phone-${provider}-pending.png`), fullPage: true });
      await mobile.getByRole("button", { name: "确认模拟支付（不会扣款）" }).click();
      await mobile.getByRole("heading", { name: "模拟支付已确认" }).waitFor();
      await mobile.reload(); await mobile.getByRole("heading", { name: "模拟支付已确认" }).waitFor();
      assert.equal(state.confirmed, 1); assert.equal(await mobile.getByRole("button").count(), 0);
      await mobile.screenshot({ path: resolve(output, `phone-${provider}-paid.png`), fullPage: true });
      assert.ok(phoneCalls.filter(path => path.endsWith("/checkout")).length >= 3);
      assert.deepEqual(await phone.cookies(), []); await phone.close();
    } else { state.paid = true; await page.getByRole("button", { name: "我已支付，读取结果" }).click(); }
    await page.getByText(/会员已生效，可返回原功能继续使用/).waitFor();
    assert.equal(state.created, 1);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("nexpoly.fixture.draft")), "CCO");
    state.legacy = true;
    await page.reload(); await page.getByText(/长期会员权限，无需重复购买/).waitFor();
    assert.equal(await page.getByLabel("月费会员套餐").count(), 0);
    assert.deepEqual(errors, []);
    report.checks.push({ width: viewport.width, provider, simulation, qr: "local PNG", phone_isolated: simulation, phone_open_refresh_read_only: simulation, explicit_confirmations: state.confirmed, paid: true, same_session_draft: "retained", legacy: "no purchase", created_orders: state.created });
    await context.close();
  }
  await writeFile(resolve(output, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} catch (error) {
  if (lastPage && !lastPage.isClosed()) {
    await lastPage.screenshot({ path: resolve(output, "failure.png"), fullPage: true }).catch(() => {});
    await writeFile(resolve(output, "failure.txt"), await lastPage.locator("body").innerText());
  }
  throw error;
} finally {
  await browser?.close(); server.kill("SIGTERM");
}
