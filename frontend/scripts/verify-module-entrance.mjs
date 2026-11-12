// Local, read-only verification: no real jobs or external iframe requests.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
const { chromium } = await import(process.env.MOTION_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MOTION_BASE_URL || "http://127.0.0.1:9001";
assert.equal(new URL(base).hostname, "127.0.0.1");
const output = resolve(process.env.MOTION_ARTIFACT_DIR || "/tmp/nexpoly-module-serial");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.MOTION_CHROMIUM_PATH || undefined, headless: true, args: ["--no-sandbox"] });
const results = [];

const groupByModule = Object.fromEntries([
  ["discover", ["knowledge", "polytaoGeneration", "monomerRetrosynthesis", "explorer", "databaseQuery", "databaseFilter", "database"]],
  ["build", ["homopolymerPrediction", "monomerPolymerization", "mdSimulationDemo", "monomerMdSimulation", "monomerDft", "conditionalGeneration"]],
  ["optimize", ["reverseDesign", "highThroughputWorkflowDemo"]]
].flatMap(([group, modules]) => modules.map(module => [module, group])));
async function sidebar(page, width) {
  if (width < 1024 && !(await page.locator("#np-mobile-navigation").count())) await page.getByRole("button", { name: "打开导航", exact: true }).click();
  return page.locator(width < 1024 ? "#np-mobile-navigation" : ".np-sidebar-desktop");
}
async function prepareChoice(page, width, module) {
  if (["home", "structureWorkbench", "researchTools"].includes(module)) {
    const nav = await sidebar(page, width);
    return nav.locator(`[data-module-id="${module}"]`);
  }
  const groupId = groupByModule[module];
  assert.ok(groupId, `Known research category for ${module}`);
  if (!(await page.locator('[data-module-content="researchTools"][data-module-phase="idle"]:not([inert])').count())) {
    const nav = await sidebar(page, width);
    await nav.locator('[data-module-id="researchTools"]').click();
    await settled(page, "researchTools");
  }
  await page.getByRole("heading", { name: "实验工具", exact: true }).waitFor();
  const group = page.locator(`main [data-research-group="${groupId}"]`);
  const reopen = group.locator('.np-research-tools__collapse[aria-expanded="false"]');
  if (await reopen.count()) await reopen.click();
  const expand = group.locator('.np-research-tools__expand:not(.np-research-tools__collapse)[aria-expanded="false"]');
  if (await expand.count()) await expand.click();
  return group.locator(`[data-module-id="${module}"]`);
}
async function choose(page, width, module) {
  const target = await prepareChoice(page, width, module);
  await target.click();
}
async function settled(page, module) {
  await page.locator(`[data-module-content="${module}"][data-module-phase="idle"]:not([inert])`).waitFor();
}
async function cycle(page, width, module) {
  const target = await prepareChoice(page, width, module);
  const begin = await page.evaluate(() => ({ animations: window.__motion.animations.length, frames: window.__motion.frames.length, module: document.querySelector("[data-module-content]").dataset.moduleContent }));
  await target.click(); await settled(page, module);
  const run = await page.evaluate((begin) => ({ animations: window.__motion.animations.slice(begin.animations), frames: window.__motion.frames.slice(begin.frames),
    finalOpacity: getComputedStyle(document.querySelector("[data-module-content]")).opacity, focused: document.activeElement === document.querySelector("main") }), begin);
  assert.equal(run.animations.length, 2, `${module}: one exit and one entry`);
  const [exit, enter] = run.animations;
  assert.equal(exit.id, "np-module-exit"); assert.equal(enter.id, "np-module-enter");
  assert.deepEqual(exit.keyframes, [{ opacity: 1 }, { opacity: 0 }]);
  assert.deepEqual(enter.keyframes, [{ opacity: 0 }, { opacity: 1 }]);
  for (const animation of run.animations) { assert.equal(animation.options.duration, 400); assert.equal(animation.options.easing, "ease-in-out"); }
  assert.ok(run.frames.some((f) => f.phase === "exiting" && f.module === begin.module && f.opacity > .05 && f.opacity < .95), `${module}: visible old-page exit`);
  const blank = run.frames.filter((f) => f.phase === "blank" && f.module === module && f.opacity === 0);
  assert.ok(blank.length, `${module}: completely blank frames`);
  assert.ok(enter.start - blank[0].time >= 190, `${module}: 200ms blank (10ms sampling tolerance)`);
  assert.ok(run.frames.some((f) => f.phase === "entering" && f.opacity > .05 && f.opacity < .95), `${module}: visible new-page entry`);
  assert.ok(run.frames.filter((f) => ["exiting", "blank", "entering"].includes(f.phase)).every((f) => f.inert), `${module}: transparent content stays inert`);
  assert.ok(run.frames.every((f) => f.transform === "none" && !f.horizontalOverflow), `${module}: stable workspace, no horizontal scrollbar`);
  assert.equal(run.finalOpacity, "1");
  if (width < 1024) assert.equal(run.focused, true, "mobile focus reaches settled content");
  return { module, exitMs: 400, gapObservedMs: Math.round(enter.start - blank[0].time), entryMs: 400, blankFrames: blank.length, samples: run.frames.length };
}
async function stopTrace(client) {
  const completion = new Promise((done) => client.once("Tracing.tracingComplete", done));
  await client.send("Tracing.end"); const { stream } = await completion;
  let trace = "";
  while (true) { const p = await client.send("IO.read", { handle: stream }); trace += p.data; if (p.eof) break; }
  await client.send("IO.close", { handle: stream });
  await writeFile(resolve(output, "module-switch.trace.json"), trace);
}
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 390, height: 844 }, { width: 2560, height: 1440 }]
    .filter((v) => !process.env.MOTION_VIEWPORT || process.env.MOTION_VIEWPORT.split(",").map(Number).includes(v.width))) {
    const context = await browser.newContext({ viewport, reducedMotion: "no-preference" });
    await context.route("**/*", route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== new URL(base).origin || !["GET", "HEAD"].includes(request.method())) return route.abort();
      if (url.pathname === "/api/v1/auth/session") return route.fulfill({ json: { authenticated: true,
        user: { id: "module-motion-fixture", username: "模块切换测试", must_change_password: false },
        session_id: "module-motion-session", csrf_token: "module-motion-csrf", capabilities: {} } });
      return route.continue();
    });
    const page = await context.newPage(); page.setDefaultTimeout(25000);
    const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() => {
      window.__motion = { animations: [], frames: [], longTasks: [] };
      const animate = Element.prototype.animate;
      Element.prototype.animate = function (keyframes, options) {
        const animation = animate.call(this, keyframes, options);
        if (options?.id?.startsWith("np-module-")) {
          const record = { id: options.id, keyframes, options, start: performance.now(), module: this.dataset.moduleContent, finished: null };
          window.__motion.animations.push(record);
          animation.addEventListener("finish", () => { record.finished = performance.now(); });
        }
        return animation;
      };
      const sample = () => {
        const content = document.querySelector("[data-module-content]");
        if (content) {
          const style = getComputedStyle(content);
          window.__motion.frames.push({ time: performance.now(), module: content.dataset.moduleContent, phase: content.dataset.modulePhase,
            opacity: Number(style.opacity), transform: style.transform, inert: content.hasAttribute("inert"), horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1 });
        }
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) window.__motion.longTasks.push({ start: entry.startTime, duration: entry.duration, phase: document.querySelector("[data-module-content]")?.dataset.modulePhase });
      }).observe({ type: "longtask", buffered: true });
    });
    const result = { viewport, modules: [] };
    let client; let tracing = false; const filmstrip = [];
    let releaseSlowRequest;
    try {
      await page.goto(`${base}/structure-workbench`);
      await page.waitForFunction(() => document.querySelector("[data-structure-editor]")?.dataset.editorStatus === "ready", null, { timeout: 30000 });
      assert.equal(await page.evaluate(() => window.__motion.animations.length), 0, "No first-load transition");
      await page.evaluate(() => { window.__retainedCanvas = document.querySelector("[data-structure-editor]"); });
      await prepareChoice(page, viewport.width, "knowledge");
      if (viewport.width === 1440) {
        client = await context.newCDPSession(page);
        await client.send("Tracing.start", { categories: "devtools.timeline,blink.user_timing,v8.execute", transferMode: "ReturnAsStream" }); tracing = true;
        if (process.env.MOTION_CAPTURE_FRAMES === "true") {
          const capture = client;
          capture.on("Page.screencastFrame", async (event) => {
            filmstrip.push({ data: event.data, timestamp: event.metadata.timestamp });
            await capture.send("Page.screencastFrameAck", { sessionId: event.sessionId }).catch(() => {});
          });
          await client.send("Page.startScreencast", { format: "jpeg", quality: 85, maxWidth: 1440, maxHeight: 900, everyNthFrame: 1 });
        }
      }
      result.modules.push(await cycle(page, viewport.width, "knowledge"));
      result.modules.push(await cycle(page, viewport.width, "structureWorkbench"));
      result.retainedCanvas = await page.evaluate(() => window.__retainedCanvas === document.querySelector("[data-structure-editor]"));
      assert.equal(result.retainedCanvas, true);
      if (client) {
        if (process.env.MOTION_CAPTURE_FRAMES === "true") await client.send("Page.stopScreencast");
        await stopTrace(client); tracing = false; await client.detach(); client = null;
      }
      if (viewport.width === 1440) {
        for (const module of ["polytaoGeneration", "explorer", "databaseQuery", "databaseFilter", "database", "homopolymerPrediction", "monomerPolymerization", "mdSimulationDemo", "monomerMdSimulation", "monomerDft", "conditionalGeneration", "reverseDesign", "highThroughputWorkflowDemo", "home"]) result.modules.push(await cycle(page, viewport.width, module));
        // Rapid retargeting uses the three persistent sidebar entries. Research
        // siblings are now cards in inert page content during transitions.
        await choose(page, viewport.width, "researchTools");
        await page.waitForFunction(() => document.querySelector("[data-module-content]")?.dataset.modulePhase === "exiting");
        await page.evaluate(() => {
          document.querySelector('.np-sidebar-desktop [data-module-id="home"]').click();
          document.querySelector('.np-sidebar-desktop [data-module-id="structureWorkbench"]').click();
        });
        await settled(page, "structureWorkbench"); result.latestTarget = true;
        await choose(page, viewport.width, "researchTools");
        await page.waitForFunction(() => document.querySelector('[data-module-content="researchTools"]')?.dataset.modulePhase === "blank");
        await page.evaluate(() => document.querySelector('.np-sidebar-desktop [data-module-id="home"]').click());
        await settled(page, "home"); result.blankReplacement = true;
        await choose(page, viewport.width, "researchTools");
        await page.waitForFunction(() => {
          const content = document.querySelector('[data-module-content="researchTools"]');
          const opacity = content ? Number(getComputedStyle(content).opacity) : 0;
          return content?.dataset.modulePhase === "entering" && opacity > .1 && opacity < .8;
        });
        await page.evaluate(() => {
          for (const module of ["home", "structureWorkbench", "home"]) document.querySelector(`.np-sidebar-desktop [data-module-id="${module}"]`).click();
        });
        await settled(page, "home"); result.latestDuringEntry = true;
        const historyLength = await page.evaluate(() => history.length);
        await page.goBack(); await settled(page, "researchTools");
        await page.goForward(); await settled(page, "home");
        assert.equal(await page.evaluate(() => history.length), historyLength);
        result.historyNavigation = true;
        let slowRequested = false;
        const slowGate = new Promise((resolve) => { releaseSlowRequest = resolve; });
        await page.route("**/monomer-md/status", async (route) => {
          slowRequested = true;
          await slowGate;
          await route.fulfill({ status: 503, json: { detail: "Read-only slow request fixture" } }).catch(() => {});
        });
        result.modules.push(await cycle(page, viewport.width, "monomerMdSimulation"));
        assert.equal(slowRequested, true);
        assert.equal(await page.getByText("服务检查中", { exact: true }).first().isVisible(), true, "The page enters while its real loading state is still pending");
        releaseSlowRequest(); releaseSlowRequest = undefined;
        await page.unroute("**/monomer-md/status");
        result.slowRequestDoesNotHoldEntry = true;
      }
      await page.screenshot({ path: resolve(output, `${viewport.width}-final.png`) });
      await choose(page, viewport.width, "databaseQuery");
      await page.waitForFunction(() => document.querySelector("[data-module-content]")?.dataset.modulePhase === "exiting");
      await page.emulateMedia({ reducedMotion: "reduce" }); await settled(page, "databaseQuery");
      assert.equal(await page.locator("[data-module-content]").evaluate((e) => getComputedStyle(e).opacity), "1");
      const count = await page.evaluate(() => window.__motion.animations.length);
      await choose(page, viewport.width, "knowledge"); await settled(page, "knowledge");
      assert.equal(await page.evaluate(() => window.__motion.animations.length), count);
      result.reducedMotion = true; result.pageErrors = errors; assert.equal(errors.length, 0); result.passed = true;
    } catch (error) {
      result.error = String(error); result.passed = false;
      await page.screenshot({ path: resolve(output, `${viewport.width}-failure.png`) }).catch(() => {});
    } finally {
      releaseSlowRequest?.();
      if (client) {
        await client.send("Page.stopScreencast").catch(() => {});
        if (tracing) await stopTrace(client).catch(() => {});
        await client.detach();
      }
      if (filmstrip.length) {
        for (const [index, f] of filmstrip.entries()) await writeFile(resolve(output, `frame-${String(index).padStart(3, "0")}.jpg`), Buffer.from(f.data, "base64"));
        await writeFile(resolve(output, "filmstrip.json"), JSON.stringify(filmstrip.map(({ timestamp }, index) => ({ file: `frame-${String(index).padStart(3, "0")}.jpg`, timestamp })), null, 2));
      }
      await writeFile(resolve(output, `${viewport.width}-samples.json`), JSON.stringify(await page.evaluate(() => window.__motion).catch(() => ({})), null, 2));
      results.push(result); console.log(JSON.stringify(result)); await context.close();
    }
  }
} finally { await browser.close(); }
await writeFile(resolve(output, "results.json"), JSON.stringify(results, null, 2));
if (results.some((r) => !r.passed)) process.exitCode = 1;
