// Read-only local browser regression. Retrosynthesis responses are fixtures;
// all other non-GET/HEAD requests and all external requests are blocked.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const { chromium } = await import(process.env.DRAWER_PLAYWRIGHT_MODULE || process.env.MOTION_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.DRAWER_BASE_URL || "http://127.0.0.1:9001";
assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(new URL(base).hostname), "Use a local frontend only");
const output = resolve(process.env.DRAWER_ARTIFACT_DIR || "/tmp/nexpoly-workbench-drawer-focus");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true,
  executablePath: process.env.DRAWER_CHROMIUM_PATH || process.env.MOTION_CHROMIUM_PATH || undefined,
  args: ["--no-sandbox"] });
const results = [];

function fixture(total) {
  return { input_smiles: "CCO", canonical_smiles: "CCO", target_role: "auto", inferred_target_role: "other", query_time_ms: 1, total,
    candidates: Array.from({ length: total }, (_, index) => ({ rank: index + 1, raw_output: "C.CO", reactants_smiles: "C.CO",
      canonical_reactants_smiles: "C.CO", valid_smiles: true, all_reactants_smaller_than_target: true, reaction_hint: "Local browser fixture",
      reactants: ["C", "CO"].map((smiles, index) => ({ input_smiles: smiles, canonical_smiles: smiles, valid_smiles: true, heavy_atom_count: index + 1 })) })) };
}

async function snapshot(page) {
  return page.evaluate(() => {
    const drawer = document.querySelector(".np-sw-drawer");
    const title = Array.from(document.querySelectorAll(".np-module-page-title, .np-research-tools__intro h1")).find(element => element.getBoundingClientRect().width > 0);
    const main = document.querySelector(".np-app-shell__body > main").getBoundingClientRect();
    const titleBox = title.getBoundingClientRect();
    const active = document.activeElement;
    return { phase: drawer?.dataset.motionPhase, focusInDrawer: Boolean(drawer?.contains(active)),
      focusedLabel: active.getAttribute("aria-label") || (active.tagName === "BUTTON" ? active.textContent?.trim() : null) || active.tagName,
      focusedInert: Boolean(active.closest("[inert]")), title: { x: titleBox.x - main.x, y: titleBox.y - main.y } };
  });
}

async function expectDrawerFocus(page) {
  await page.waitForFunction(() => document.querySelector(".np-sw-drawer")?.dataset.motionPhase === "open");
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "关闭单体反推结果");
  // Catch a later popover cleanup or stale frame stealing initial focus back.
  await page.waitForTimeout(250);
  const state = await snapshot(page);
  assert.equal(state.focusInDrawer, true);
  assert.equal(state.focusedLabel, "关闭单体反推结果");
  assert.equal(state.focusedInert, false);
  return state;
}

async function expectClosed(page, expectedFocus = "展开反推结果") {
  await page.waitForFunction(() => document.querySelector(".np-sw-drawer")?.dataset.motionPhase === "closed");
  await page.waitForFunction(label => {
    const active = document.activeElement;
    return (active?.getAttribute("aria-label") || active?.textContent?.trim()) === label;
  }, expectedFocus);
  await page.waitForTimeout(150);
  const state = await snapshot(page);
  assert.equal(state.focusInDrawer, false);
  assert.equal(state.focusedLabel, expectedFocus);
  assert.equal(state.focusedInert, false);
  return state;
}

try {
  for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 900 }]) {
    for (const reducedMotion of ["reduce", "no-preference"]) {
      for (const total of [0, 2]) {
        const name = `${viewport.width}-${reducedMotion}-${total}`;
        const context = await browser.newContext({ viewport, reducedMotion });
        let fixtureRequests = 0;
        await context.route("**/*", route => {
          const request = route.request(), url = new URL(request.url());
          if (url.origin !== new URL(base).origin) return route.abort();
          if (url.pathname === "/api/v1/auth/session") return route.fulfill({ json: { authenticated: true,
            user: { id: "drawer-focus-fixture", username: "反推焦点测试", must_change_password: false },
            session_id: "drawer-focus-session", csrf_token: "drawer-focus-csrf", capabilities: {} } });
          if (url.pathname === "/api/v1/usage/entitlements") return route.fulfill({ json: { enabled: false } });
          if (request.method() === "POST" && url.pathname.endsWith("/monomer-retrosynthesis")) {
            fixtureRequests++;
            return route.fulfill({ json: fixture(total) });
          }
          return ["GET", "HEAD"].includes(request.method()) ? route.continue() : route.abort();
        });
        const page = await context.newPage();
        page.setDefaultTimeout(15000);
        const result = { viewport, reducedMotion, total, passed: false };
        try {
          await page.goto(`${base}/structure-workbench`);
          await page.waitForFunction(() => document.querySelector("[data-structure-editor]")?.dataset.editorStatus === "ready", null, { timeout: 45000 });
          await page.getByRole("button", { name: "功能参数", exact: true }).click();
          await page.getByRole("button", { name: "打开单体逆合成反推", exact: true }).click();
          await page.locator('[data-module-content="monomerRetrosynthesis"][data-module-phase="idle"]:not([inert])').waitFor();
          await page.getByRole("textbox", { name: "目标单体 SMILES" }).fill("CCO");
          await page.getByRole("button", { name: "运行反推", exact: true }).click();
          result.initial = await expectDrawerFocus(page);
          assert.deepEqual(result.initial.title, viewport.width < 900 ? { x: 16, y: 20 } : { x: 20, y: 22 });
          await page.keyboard.press("Shift+Tab");
          assert.equal((await snapshot(page)).focusInDrawer, true);
          await page.keyboard.press("Tab");
          assert.equal((await snapshot(page)).focusedLabel, "关闭单体反推结果");
          await page.screenshot({ path: resolve(output, `${name}-open.png`) });
          await page.keyboard.press("Escape");
          // The independent form stays visible; closing the first run restores
          // its submit button, while subsequent opens restore the reopen handle.
          result.closed = await expectClosed(page, "运行反推");
          await page.getByRole("button", { name: "展开反推结果", exact: true }).click();
          result.reopened = await expectDrawerFocus(page);
          await page.getByRole("button", { name: "关闭单体反推结果", exact: true }).click();
          await expectClosed(page);

          // Reverse the real open action before the two-frame focus callback.
          // DOM activation deliberately races rendering; it does not call focus.
          await page.evaluate(() => {
            document.querySelector('[aria-label="展开反推结果"]').click();
            requestAnimationFrame(() => document.querySelector('.np-sw-drawer [aria-label="关闭单体反推结果"]').click());
          });
          result.rapidClose = await expectClosed(page);

          // Platform navigation must remain available while this local overlay
          // opens; the retained module must cancel any pending focus afterwards.
          if (viewport.width >= 1024) {
            assert.equal(await page.locator(".np-sidebar-desktop").evaluate(element => element.inert), false);
            await page.evaluate(() => {
              document.querySelector('[aria-label="展开反推结果"]').click();
              document.querySelector('.np-sidebar-desktop [data-module-id="researchTools"]').click();
            });
          } else {
            await page.evaluate(() => {
              document.querySelector('[aria-label="展开反推结果"]').click();
              document.querySelector('[aria-label="打开导航"]').click();
            });
            await page.waitForFunction(() => document.querySelector("#np-mobile-navigation")?.dataset.motionPhase === "open");
            await page.locator('#np-mobile-navigation [data-module-id="researchTools"]').click();
          }
          await page.locator('[data-module-content="researchTools"][data-module-phase="idle"]:not([inert])').waitFor();
          await page.getByRole("heading", { name: "实验工具", exact: true }).waitFor();
          await page.waitForTimeout(250);
          result.afterNavigation = await snapshot(page);
          assert.equal(result.afterNavigation.focusInDrawer, false);
          assert.equal(result.afterNavigation.focusedInert, false);
          assert.equal(await page.locator(".np-app-shell__body > main").evaluate(element => element.inert), false);
          assert.equal(fixtureRequests, 1, "Reopening or reversing the drawer must not rerun retrosynthesis");
          result.fixtureRequests = fixtureRequests;
          result.passed = true;
        } catch (error) {
          result.error = String(error);
          result.final = await snapshot(page).catch(() => null);
          await page.screenshot({ path: resolve(output, `${name}-failure.png`) }).catch(() => {});
        } finally {
          results.push(result);
          console.log(JSON.stringify(result));
          await context.close();
        }
      }
    }
  }
} finally {
  await browser.close();
  await writeFile(resolve(output, "results.json"), JSON.stringify(results, null, 2));
}
if (results.some(result => !result.passed)) process.exitCode = 1;
