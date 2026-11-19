import assert from "node:assert/strict";
import { createHash, X509Certificate } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
const require = createRequire(new URL("../../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
export async function developmentBrowser(state, stack) {
  const A = "test-user-a";
  const authorizedContexts = [];
  const { parent, agent } = stack;
  assert.equal(parent, "https://app.m2.test:18760");
  assert.equal(agent, "https://agent.m2.test:18761");
  const cert = new X509Certificate(await readFile(resolve(state, "tls.crt")));
  const pin = createHash("sha256")
    .update(cert.publicKey.export({ type: "spki", format: "der" }))
    .digest("base64");
  const browser = await chromium.launch({
    args: [
      "--no-proxy-server",
      "--host-resolver-rules=MAP app.m2.test 127.0.0.1, MAP agent.m2.test 127.0.0.1, MAP * ~NOTFOUND",
      "--ignore-certificate-errors-spki-list=" + pin,
    ],
  });
  async function device() {
    const c = await browser.newContext({
      viewport: { width: 1550, height: 1000 },
      serviceWorkers: "block",
      locale: "zh-CN",
      proxy: { server: "http://127.0.0.1:1", bypass: "app.m2.test,agent.m2.test" },
    });
    await c.route(/^https?:\/\//, (route) =>
      [parent, agent].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort(),
    );
    return c;
  }
  async function app(c) {
    const p = await c.newPage();
    await p.goto(parent + "/", { waitUntil: "domcontentloaded" });
    return p;
  }
  async function login(p, user) {
    const sidebar = p.locator(".np-sidebar-desktop");
    await sidebar.getByRole("button", { name: "游客账号菜单", exact: true }).click({ timeout: 25000 });
    await p.getByRole("menuitem", { name: "登录", exact: true }).click();
    const form = p.getByRole("dialog", { name: "登录工作空间" });
    await form.getByLabel("用户名", { exact: true }).fill(user);
    await form
      .getByLabel("密码", { exact: true })
      .fill((await readFile(resolve(state, "credentials", user === A ? "a" : "b"), "utf8")).trim());
    await Promise.all([
      p.waitForResponse((r) => r.url() === parent + "/api/v1/auth/login" && r.status() === 200),
      form.getByRole("button", { name: "登录", exact: true }).click(),
    ]);
    await sidebar.getByRole("button", { name: "账号菜单：" + user, exact: true }).waitFor();
    authorizedContexts.push(p);
    return p.evaluate(async () => {
      const { session_id, user } = await (await fetch("/api/v1/auth/session")).json();
      return { session_id, user_id: user.id };
    });
  }
  async function logout(p, user) {
    await p
      .locator(".np-sidebar-desktop")
      .getByRole("button", { name: "账号菜单：" + user, exact: true })
      .click();
    await p.getByRole("menuitem", { name: "退出登录", exact: true }).click();
    await p
      .locator(".np-sidebar-desktop")
      .getByRole("button", { name: "游客账号菜单", exact: true })
      .waitFor();
  }
  async function workbench(p) {
    const end = Date.now() + 65000;
    while (Date.now() < end) {
      const f = p.frames().find((x) => x.url().startsWith(agent + "/") && !x.url().includes("/_nexpoly/"));
      if (
        f &&
        (await f
          .evaluate(() =>
            Boolean(
              history.state?.npPage &&
              document.querySelector('script[src*="index-production"]') &&
              document.documentElement.style.visibility !== "hidden",
            ),
          )
          .catch(() => false))
      )
        return f;
      await sleep(150);
    }
    throw Error("Authenticated November workspace did not render");
  }
  return { browser, device, app, login, logout, workbench, authorizedContexts };
}
