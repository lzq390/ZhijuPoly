// Local UI verification. Only local GET/HEAD requests may reach the server.
// No computation, upload, deletion, or other API mutation is permitted.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { taskThemeFixture } from "./page-consistency-fixtures.mjs";

const { chromium } = await import(process.env.PAGE_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.PAGE_BASE_URL || "http://127.0.0.1:9001";
assert.equal(new URL(base).hostname, "127.0.0.1");
const output = resolve(process.env.PAGE_ARTIFACT_DIR || "/tmp/nexpoly-page-consistency");
await mkdir(output, { recursive: true });
const modules = [
  ["/structure-workbench", "结构工作台"], ["/knowledge", "知识检索"],
  ["/polytao-generation", "聚合物生成"], ["/explorer", "聚合物相似性探索"],
  ["/database-query", "数据库查询"], ["/database-filter", "数据库筛选"],
  ["/database", "数据库分析"], ["/homopolymer-property-prediction", "均聚物性质预测"],
  ["/monomer-polymerization", "单体正向聚合"], ["/md-simulation", "MD 模拟"],
  ["/monomer-md-simulation", "单体 MD 模拟"], ["/monomer-dft", "单体 DFT"],
  ["/conditional-generation", "条件聚合物生成"], ["/reverse-design", "Tg 逆向设计"],
  ["/high-throughput-workflow-demo", "高通量优化演示"],
].filter(([path]) => !process.env.PAGE_PATH || process.env.PAGE_PATH.split(",").includes(path));
const viewports = [[1920,1080], [2560,1440], [1440,900], [1024,768], [390,844],
  [899,900], [900,900], [901,900], [1999,1120], [2000,1119], [2000,1120], [2560,1119]]
  .filter(([width]) => !process.env.PAGE_VIEWPORT || process.env.PAGE_VIEWPORT.split(",").map(Number).includes(width));
assert.ok(modules.length && viewports.length, "PAGE_PATH/PAGE_VIEWPORT must select at least one module and viewport");
const near = (actual, expected, message) => assert.ok(Math.abs(actual - expected) <= 1, `${message}: ${actual} != ${expected}`);
const results = [];
const failures = [];
const browser = await chromium.launch({ headless: true, executablePath: process.env.PAGE_CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });

async function geometry(page, title, viewport, state) {
  const data = await page.evaluate(() => {
    const visible = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
    const main = document.querySelector(".np-app-shell__body > main");
    const root = [...main.querySelectorAll(".np-module-page")].find(visible);
    const h1 = root.querySelector(".np-module-page-title");
    const rect = h1.getBoundingClientRect(), origin = main.getBoundingClientRect(), style = getComputedStyle(h1);
    const header = h1.parentElement.getBoundingClientRect();
    const body = root.querySelector(".np-module-page-body").getBoundingClientRect();
    const rootStyle = getComputedStyle(root);
    const surfaces = [...root.querySelectorAll('.np-sw-canvas-stage, .np-md-workbench-surface, .np-mp-surface, .np-batch-surface, .np-mmd-workbench-surface, .np-dft-workbench-surface, .dbf-filter-surface, .dba-analysis-surface, .ks-search-surface, .polytao-generation-surface, .ht-workbench-board')]
      .filter(visible).map(e => ({ radius: parseFloat(getComputedStyle(e).borderTopLeftRadius), shadow: getComputedStyle(e).boxShadow, background: getComputedStyle(e).backgroundColor }));
    return { title: h1.textContent, x: rect.x - origin.x, y: rect.y - origin.y,
      size: parseFloat(style.fontSize), line: parseFloat(style.lineHeight), weight: style.fontWeight,
      font: style.fontFamily, colour: style.color, headerHeight: header.height,
      bodyTop: body.top - origin.top, rootHeight: root.getBoundingClientRect().height, mainHeight: origin.height,
      headings: [...main.querySelectorAll("h1")].filter(visible).length,
      overflow: main.scrollWidth > main.clientWidth + 1 || root.scrollWidth > root.clientWidth + 1 || document.documentElement.scrollWidth > innerWidth + 1,
      palette: ["blue", "blue-strong", "cyan", "success", "warning", "danger"].map(key => rootStyle.getPropertyValue(`--np-theme-${key}`).trim()),
      directHeader: h1.parentElement.parentElement === root, surfaces };
  });
  const [width, height] = viewport;
  const expected = width >= 2000 && height >= 1120 ? [36,30,28,42,84] : width <= 899 ? [16,20,20,30,60] : [20,22,20,30,62];
  assert.equal(data.title, title);
  ["x", "y", "size", "line", "headerHeight"].forEach((key, i) => near(data[key], expected[i], `${title} ${state} ${key}`));
  assert.equal(data.weight, "750");
  assert.equal(data.colour, "rgb(11, 31, 58)");
  assert.ok(data.font.startsWith("Exo"));
  assert.equal(data.headings, 1);
  assert.equal(data.directHeader, true);
  assert.equal(data.overflow, false, `${title}: outer horizontal overflow`);
  near(data.rootHeight, data.mainHeight, `${title}: full-height root`);
  assert.ok(data.bodyTop >= expected[4] - 1, `${title}: body overlaps header`);
  assert.deepEqual(data.palette, ["#2563eb", "#1d4ed8", "#0891b2", "#10b981", "#f59e0b", "#dc2626"]);
  for (const surface of data.surfaces) {
    near(surface.radius, expected[2] === 28 ? 20 : 14, `${title}: surface radius`);
    assert.notEqual(surface.shadow, "none", `${title}: surface shadow`);
    assert.equal(surface.background, "rgb(255, 255, 255)", `${title}: white surface`);
  }
  return { viewport, state, ...data };
}

async function scrollAndCheck(page, title, viewport) {
  // Single/batch polymerization deliberately scrolls the whole module. No other
  // module or ancestor may move the shared title (see MASTER's explicit exception).
  const wholePage = new URL(page.url()).pathname === "/monomer-polymerization";
  const scrolls = await page.evaluate(async wholePage => {
    const root = [...document.querySelectorAll(".np-app-shell__body > main .np-module-page")]
      .find(e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    const title = root.querySelector(".np-module-page-title");
    const ancestors = [];
    for (let e = root.parentElement; e; e = e.parentElement) ancestors.push(e);
    const candidates = [root, ...ancestors, ...root.querySelectorAll("*")].filter(e => {
      const s = getComputedStyle(e);
      return !e.closest("[hidden], [inert]") && s.visibility !== "hidden" && e.clientWidth > 0
        && e.clientHeight > 0 && e.scrollHeight > e.clientHeight + 1 && /auto|scroll/.test(s.overflowY);
    });
    const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const measurements = [];
    for (const element of candidates) {
      const old = element.scrollTop;
      const before = title.getBoundingClientRect();
      try {
        element.scrollTop = old === 0 ? Math.min(240, element.scrollHeight - element.clientHeight) : 0;
        await frames();
        const after = title.getBoundingClientRect();
        measurements.push({ element: element.className || element.tagName,
          kind: element === root ? "root" : ancestors.includes(element) ? "ancestor" : "body",
          distance: element.scrollTop - old, dx: after.x - before.x, dy: after.y - before.y,
          connected: title.isConnected,
          expectedDy: wholePage && element === root ? old - element.scrollTop : 0 });
      } finally {
        element.scrollTop = old;
        await frames();
      }
    }
    return measurements;
  }, wholePage);
  for (const scroll of scrolls) {
    assert.equal(scroll.connected, true, `${title}: header detached during scroll`);
    near(scroll.dx, 0, `${title}: ${scroll.kind} horizontal title movement`);
    near(scroll.dy, scroll.expectedDy, `${title}: ${scroll.kind} vertical title movement`);
  }
  if (wholePage) assert.ok(scrolls.some(s => s.kind === "root" && s.distance > 0), `${title}: whole-page scrolling must be exercised`);
  const data = await geometry(page, title, viewport, "scrolled");
  return { ...data, scrollContract: wholePage ? "whole-page" : "fixed-header",
    scrolledRegions: scrolls.filter(s => s.distance !== 0).length, scrolls };
}

async function settleModule(page, path) {
  if (path === "/monomer-polymerization") {
    // Service capabilities introduce the batch provider around the workbench,
    // replacing its DOM root. Initial geometry is checked while loading; start
    // deliberate scrolling only after that provider has been mounted.
    await page.locator(".np-mp-service-status > span.is-loading").waitFor({ state: "hidden" });
  }
}

async function knowledgeModes(page, viewport) {
  let current = "local";
  for (const next of ["online", "local", "pdf", "local", "online", "pdf", "local"]) {
    await page.locator(`#knowledge-tab-${current}-${next}`).click();
    const panels = await page.locator(".ks-mode-panel").evaluateAll(elements => elements.map(e => ({
      id: e.id, hidden: e.hidden, inert: e.inert, ariaHidden: e.getAttribute("aria-hidden"),
      display: getComputedStyle(e).display, rects: e.getClientRects().length,
      selected: e.querySelector('[role="tab"][aria-selected="true"]')?.getAttribute("aria-controls")
    })));
    for (const panel of panels) {
      const active = panel.id === `knowledge-panel-${next}`;
      assert.equal(panel.hidden, !active, `${panel.id}: hidden`);
      assert.equal(panel.inert, !active, `${panel.id}: inert`);
      assert.equal(panel.ariaHidden, String(!active), `${panel.id}: aria-hidden`);
      if (active) {
        assert.notEqual(panel.display, "none");
        assert.ok(panel.rects > 0);
        assert.equal(panel.selected, panel.id, "selected tab must match the displayed panel");
      } else {
        assert.equal(panel.display, "none", `${panel.id}: inactive panel must not paint`);
        assert.equal(panel.rects, 0, `${panel.id}: inactive panel must have no layout boxes`);
      }
    }
    results.push({ ...await geometry(page, "知识检索", viewport, `mode-${current}-${next}`), panels });
    current = next;
  }
  await page.screenshot({ path: resolve(output, `knowledge-round-trip-${viewport.join("-")}.png`) });
}

async function taskStatusThemes(page, path, title, viewport) {
  const fixture = taskThemeFixture(path);
  const handler = route => {
    const url = new URL(route.request().url());
    const payload = fixture.response(url);
    return route.fulfill(payload === undefined ? { status: 503, json: { detail: "Local UI fixture" } } : { json: payload });
  };
  await page.route("**/api/**", handler);
  try {
    await page.goto(base + path, { waitUntil: "domcontentloaded" });
    await page.locator('[role="tab"]').filter({ hasText: /任务/ }).first().click();
    const styles = [];
    for (const [status, role] of fixture.roles) {
      const badge = page.locator(`${fixture.selector}.is-${status}`).first();
      await badge.waitFor();
      const data = await badge.evaluate((element, role) => {
        const root = element.closest(".np-module-page");
        const reference = document.createElement("span");
        reference.style.cssText = `color:var(--np-theme-${role}-text);background:var(--np-theme-${role}-soft);border:1px solid var(--np-theme-${role}-border);font-family:var(--font-body)`;
        root.append(reference);
        const read = node => { const s = getComputedStyle(node); return { colour: s.color, background: s.backgroundColor, border: s.borderTopColor, image: s.backgroundImage, font: s.fontFamily }; };
        try { return { actual: read(element), expected: read(reference) }; }
        finally { reference.remove(); }
      }, role);
      assert.deepEqual(data.actual, data.expected, `${title}: ${status} must use the shared ${role} role`);
      styles.push({ status, role, ...data.actual });
    }
    results.push({ ...await geometry(page, title, viewport, "task-status-themes"), styles });
    await page.screenshot({ path: resolve(output, `${path.slice(1)}-task-states-${viewport.join("-")}.png`) });
  } finally {
    await page.unroute("**/api/**", handler);
  }
}

async function tabs(page, selector, title, viewport) {
  const count = await page.locator(`${selector} [role="tab"]:visible`).count();
  for (let i = 0; i < count; i++) {
    const tab = page.locator(`${selector} [role="tab"]:visible`).nth(i);
    if (await tab.isDisabled()) continue;
    await tab.click();
    results.push(await geometry(page, title, viewport, `tab-${i}`));
  }
}

async function serviceFailure(page, path, selector, title, viewport) {
  const failure = route => route.fulfill({ status: 503, json: { detail: "Local UI state fixture" } });
  await page.route("**/api/**", failure);
  try {
    await page.goto(`${base}${path}?mode=single`, { waitUntil: "domcontentloaded" });
    const badge = page.locator(`${selector} > span.is-error`);
    await badge.waitFor();
    await page.waitForFunction(selector => {
      const badge = document.querySelector(`${selector} > span.is-error`);
      return badge && getComputedStyle(badge).color === "rgb(153, 27, 27)";
    }, selector);
    const style = await badge.evaluate(element => {
      const computed = getComputedStyle(element);
      return { colour: computed.color, background: computed.backgroundColor, border: computed.borderTopColor };
    });
    assert.deepEqual(style, { colour: "rgb(153, 27, 27)", background: "rgb(254, 242, 242)", border: "rgba(220, 38, 38, 0.24)" });
    results.push({ ...await geometry(page, title, viewport, "service-failure"), serviceStyle: style });
  } finally {
    await page.unroute("**/api/**", failure);
  }
}

async function highThroughputStages(page, viewport) {
  const title = "高通量优化演示";
  await page.getByRole("button", { name: "确认场景设置，进入 S1", exact: true }).click();
  results.push(await geometry(page, title, viewport, "S1"));
  for (const target of ["Tg", "CTE", "Elongation", "Modulus"]) {
    await page.getByRole("button", { name: `展开 ${target} Agent`, exact: true }).click();
    await page.getByRole("button", { name: `上传样例（${target}）`, exact: true }).click();
    // Narrow workbenches open one Agent at a time; the next toggle closes it.
  }
  await page.getByRole("button", { name: "确认先验，进入 S2", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.ht-flow-steps [aria-current="step"] span')?.textContent === "S2");
  const visited = new Set();
  for (let step = 0; step < 20; step++) {
    const stage = (await page.locator('.ht-flow-steps [aria-current="step"] span').first().textContent()).trim();
    visited.add(stage);
    results.push(await geometry(page, title, viewport, stage));
    results.push(await scrollAndCheck(page, title, viewport));
    if (stage === "S6") break;
    const confirm = page.getByRole("button", { name: /^(确认本批 [84] 项验证值|确认本步 4 项验证值)$/ });
    if (await confirm.count()) await confirm.click();
    const next = stage === "S2" ? page.getByRole("button", { name: "进入 S3", exact: true })
      : stage === "S3" ? page.getByRole("button", { name: /^(进入 R2|进入收敛|进入 S4 候选输出)$/ })
      : stage === "S4" ? page.getByRole("button", { name: "进入 S5 多目标配比搜索", exact: true })
      : page.locator(".ht-s5-footer .ht-s1-primary-button");
    await next.click();
    await page.waitForTimeout(1600); // Existing demo stage transition, not a backend task.
  }
  assert.deepEqual([...visited], ["S2", "S3", "S4", "S5", "S6"]);
}

try {
  for (const viewport of viewports) {
    const [width, height] = viewport;
    const context = await browser.newContext({ viewport: { width, height }, reducedMotion: "reduce" });
    await context.route("**/*", route => new URL(route.request().url()).origin === base && ["GET", "HEAD"].includes(route.request().method()) ? route.continue() : route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    const lifecycle = [];
    page.on("framenavigated", frame => { if (frame === page.mainFrame()) lifecycle.push({ event: "navigation", url: frame.url() }); });
    page.on("console", message => { if (message.text().includes("[vite]")) lifecycle.push({ event: "vite", message: message.text() }); });
    page.on("pageerror", error => errors.push(error.message));
    for (const [path, title] of modules) {
      try {
        await page.goto(base + path, { waitUntil: "domcontentloaded" });
        await page.locator(".np-module-page-title:visible").waitFor();
        results.push(await geometry(page, title, viewport, "initial"));
        await page.waitForTimeout(250);
        await settleModule(page, path);
        results.push(await geometry(page, title, viewport, "settled"));
        if ([1920,2560,390].includes(width) && ["/knowledge","/polytao-generation","/database-filter","/structure-workbench","/monomer-dft","/high-throughput-workflow-demo"].includes(path)) {
          if (path === "/structure-workbench") await page.waitForFunction(() => {
            const editor = document.querySelector('[data-structure-editor]');
            return editor ? editor.dataset.editorStatus === "ready" : Boolean(document.querySelector('.np-sw-editor iframe')?.contentWindow?.ketcher);
          });
          await page.screenshot({ path: resolve(output, `${path.slice(1)}-${width}-${height}.png`) });
        }
        results.push(await scrollAndCheck(page, title, viewport));
        if (path === "/monomer-polymerization") {
          for (const mode of ["batch", "single"]) {
            await page.goto(`${base}${path}?mode=${mode}`, { waitUntil: "domcontentloaded" });
            await page.locator(".np-module-page-title:visible").waitFor();
            await settleModule(page, path);
            results.push(await geometry(page, title, viewport, mode));
            results.push({ ...await scrollAndCheck(page, title, viewport), state: `${mode}-scrolled` });
            if (mode === "batch" && [1920,2560,390].includes(width)) {
              await page.screenshot({ path: resolve(output, `monomer-polymerization-batch-${width}-${height}.png`) });
              await page.getByRole("tab", { name: "单次聚合", exact: true }).click();
              results.push(await geometry(page, title, viewport, "switch-to-single"));
            }
          }
        }
        if ([1920,2560,390].includes(width)) {
          if (path === "/knowledge") await knowledgeModes(page, viewport);
          const selectors = { "/monomer-md-simulation": ".np-mmd-main-tabs", "/monomer-dft": ".np-dft-main-tabs", "/md-simulation": ".np-md-workspace-tabs" };
          if (selectors[path]) await tabs(page, selectors[path], title, viewport);
          const services = { "/monomer-polymerization": ".np-mp-service-status", "/md-simulation": ".np-md-service-status", "/monomer-md-simulation": ".np-mmd-service-status", "/monomer-dft": ".np-dft-service-status" };
          if (services[path]) await serviceFailure(page, path, services[path], title, viewport);
          if (["/monomer-md-simulation", "/monomer-dft"].includes(path)) await taskStatusThemes(page, path, title, viewport);
          if (path === "/database") for (const dataset of ["process","property","structure-effect","dft","formulation"]) {
            await page.goto(`${base}/database/${dataset}`, { waitUntil: "domcontentloaded" });
            await page.locator(".np-module-page-title:visible").waitFor();
            results.push(await geometry(page, title, viewport, dataset));
          }
          if (path === "/high-throughput-workflow-demo" && process.env.PAGE_STAGES !== "false") await highThroughputStages(page, viewport);
        }
      } catch (error) {
        const failure = { viewport, path, error: String(error), lifecycle: lifecycle.slice(-6) };
        failures.push(failure);
        console.error(JSON.stringify(failure));
      }
    }
    failures.push(...errors.map(error => ({ viewport, error })));
    await context.close();
    // Preserve completed viewports even if a long local run is interrupted.
    await writeFile(resolve(output, "report.json"), JSON.stringify({ results, failures }, null, 2));
    console.log(`${width}×${height}: checked ${modules.length} modules; ${failures.length} failures so far`);
  }
} finally {
  await browser.close();
  await writeFile(resolve(output, "report.json"), JSON.stringify({ results, failures }, null, 2));
}
assert.equal(failures.length, 0, JSON.stringify(failures, null, 2));
console.log(`Passed ${results.length} geometry/state checks. Artifacts: ${output}`);
