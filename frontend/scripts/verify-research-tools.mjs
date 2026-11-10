// Verify served UI using synthetic identity and intercepted APIs; never submit jobs.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = new URL(process.env.RESEARCH_BASE_URL || "http://127.0.0.1:9001").origin;
const expectedWorkspace = process.env.RESEARCH_EXPECT_WORKSPACE;
const verifyRefinement = process.env.RESEARCH_VERIFY_REFINEMENT === "1";
const output = resolve(process.env.RESEARCH_ARTIFACT_DIR || "/tmp/nexpoly-research-tools");
await mkdir(output, { recursive: true });
const identity = { authenticated: true, account_type: "member",
  user: { id: "research-tools-fixture", username: "界面验收", must_change_password: false },
  session_id: "fixture-research-tools", csrf_token: "fixture-only", capabilities: { "openscience.use": true } };
const modules = [
  ["knowledge", "/knowledge"], ["polytaoGeneration", "/polytao-generation"],
  ["monomerRetrosynthesis", "/monomer-retrosynthesis"], ["explorer", "/explorer"],
  ["databaseQuery", "/database-query"], ["databaseFilter", "/database-filter"], ["database", "/database"],
  ["homopolymerPrediction", "/homopolymer-property-prediction"], ["monomerPolymerization", "/monomer-polymerization"],
  ["mdSimulationDemo", "/md-simulation"], ["monomerMdSimulation", "/monomer-md-simulation"],
  ["monomerDft", "/monomer-dft"], ["conditionalGeneration", "/conditional-generation"],
  ["reverseDesign", "/reverse-design"], ["highThroughputWorkflowDemo", "/high-throughput-workflow-demo"]
];
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
const report = { syntheticIdentity: true, backendRequests: 0, verifyRefinement, cases: [] };
try {
  const viewports = [{ width: 1440, height: 1050 }, { width: 1920, height: 1080 }, { width: 390, height: 844 }, { width: 320, height: 740 }];
  if (verifyRefinement) viewports.push({ width: 2200, height: 1200 });
  for (const viewport of viewports) {
    const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
    const errors = [], blockedWrites = [], reads = [], scripts = [], gifRequests = [], gifResponses = [];
    await context.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      if (!["GET", "HEAD"].includes(request.method())) { blockedWrites.push(request.method()+" "+url.pathname); return route.abort(); }
      if (request.isNavigationRequest() && request.frame().parentFrame()) return route.fulfill({ contentType: "text/html", body: "<!doctype html><html lang=zh><body>智能助手工作空间</body></html>" });
      if (url.origin !== base) return route.abort();
      if (url.pathname.startsWith("/api/")) {
        reads.push(url.pathname);
        return route.fulfill({ status: url.pathname === "/api/v1/auth/session" ? 200 : 503,
          json: url.pathname === "/api/v1/auth/session" ? identity : { detail: "Read-only interface fixture" } });
      }
      if (/^\/previews\/research-tools\/[^/]+\.gif$/.test(url.pathname)) gifRequests.push(url.pathname);
      if (request.resourceType() === "script") scripts.push(url.pathname);
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on("pageerror", error=>errors.push(error.message));
    page.on("response", response => {
      const path = new URL(response.url()).pathname;
      if (!/^\/previews\/research-tools\/[^/]+\.gif$/.test(path)) return;
      gifResponses.push(response.body().then(body => ({ path, status: response.status(), contentType: response.headers()["content-type"],
        signature: body.subarray(0, 6).toString("ascii"), bytes: body.length })).catch(error => ({ path, error: error.message })));
    });
    const group = id=>page.locator(`[data-research-group="${id}"]`);
    const counts = async()=>Promise.all(["discover","build","optimize"].map(id=>group(id).locator("button[data-module-id]:visible").count()));
    async function settled(module) { await page.locator(`[data-module-content="${module}"][data-module-phase="idle"]:not([inert])`).waitFor(); }
    async function sidebar() {
      if (viewport.width >= 1024) return page.locator(".np-sidebar-desktop");
      const dialog = page.getByRole("dialog",{ name: "平台导航" });
      if (!(await dialog.isVisible())) await page.getByRole("button", { name: "打开导航", exact: true }).click();
      return dialog;
    }
    async function directory() {
      await page.mouse.move(0, 0);
      const nav=await sidebar();
      await nav.locator('[data-module-id="researchTools"]').click();
      await settled("researchTools");
      await group("discover").waitFor();
    }
    async function assertNoOverflow() {
      const overflow = await page.evaluate(()=>({ page:document.documentElement.scrollWidth > innerWidth+1,
        main:document.querySelector("main").scrollWidth > document.querySelector("main").clientWidth+1 }));
      assert.deepEqual(overflow, { page:false, main:false });
    }
    async function titleStyle(selector) {
      await page.locator(selector).waitFor();
      await page.evaluate(() => document.fonts.ready);
      return page.locator(selector).evaluate(element => {
        const style = getComputedStyle(element);
        return { text: element.textContent, fontSize: style.fontSize, lineHeight: style.lineHeight,
          fontWeight: style.fontWeight, fontFamily: style.fontFamily, letterSpacing: style.letterSpacing };
      });
    }
    async function verifySelectionPlayback() {
      const images = page.locator(".np-research-tool__thumbnail img");
      const first = group("discover").getByRole("button", { name: "知识检索", exact: true });
      const second = group("build").getByRole("button", { name: "均聚物性质预测", exact: true });
      async function loaded(module) {
        const image = page.locator(`.np-research-tool__thumbnail img[src$="/${module}.gif"]`);
        await image.waitFor({ state: "visible" });
        await page.waitForFunction(id => {
          const image = document.querySelector(`.np-research-tool__thumbnail img[src$="/${id}.gif"]`);
          return image?.complete && image.naturalWidth > 0 && image.naturalHeight > 0;
        }, module);
        assert.equal(await images.count(), 1, "only the selected card mounts a GIF image");
        const responses = await Promise.all(gifResponses);
        const response = responses.find(item => item.path === `/previews/research-tools/${module}.gif`);
        assert.ok(response, `${module}: selected GIF has a network response`);
        assert.equal(response.error, undefined, `${module}: GIF body can be read`);
        assert.equal(response.status, 200, `${module}: GIF response succeeds`);
        assert.match(response.contentType || "", /^image\/gif(?:;|$)/i, `${module}: served as image/gif`);
        assert.match(response.signature, /^GIF8[79]a$/, `${module}: actual GIF bytes, not an SVG renamed .gif`);
        assert.ok(response.bytes > 1000, `${module}: nonempty GIF animation asset`);
      }
      assert.deepEqual(gifRequests, [], "directory initially makes zero GIF requests");
      assert.equal(await images.count(), 0, "default card previews are static");
      await first.hover();
      await loaded("knowledge");
      assert.equal(new URL(page.url()).pathname, "/research-tools", "hovering the whole card previews without navigating");
      assert.deepEqual(gifRequests, ["/previews/research-tools/knowledge.gif"]);
      const imageHandle = await images.elementHandle();
      await first.locator(".np-research-tool__title").hover();
      assert.ok(await images.evaluate((image, original) => image === original, imageHandle), "moving between children preserves the GIF element and playback");
      assert.equal(await images.isVisible(), true, "moving within the card keeps the loaded GIF visible");
      assert.deepEqual(gifRequests, ["/previews/research-tools/knowledge.gif"], "moving within the card does not request the GIF again");
      await imageHandle.dispose();
      const screenshot = `research-tools-hover-gif-${viewport.width}.png`;
      await page.screenshot({ path: resolve(output, screenshot) });
      await second.hover();
      await loaded("homopolymerPrediction");
      assert.equal(await first.locator("img").count(), 0, "hovering a second card stops the first");
      assert.deepEqual(gifRequests, ["/previews/research-tools/knowledge.gif", "/previews/research-tools/homopolymerPrediction.gif"]);
      await page.mouse.move(0, 0);
      await images.waitFor({ state: "detached" });
      assert.equal(await images.count(), 0, "leaving an unfocused card stops playback");
      await first.focus();
      await loaded("knowledge");
      await first.locator(".np-research-tool__thumbnail").hover();
      const focusedImage = await images.elementHandle();
      await first.locator(".np-research-tool__copy").hover();
      await page.mouse.move(0, 0);
      assert.ok(await images.evaluate((image, original) => image === original, focusedImage), "leaving the still-focused card keeps its existing preview playing");
      assert.equal(await images.isVisible(), true);
      await focusedImage.dispose();
      await first.evaluate(button => button.blur());
      await images.waitFor({ state: "detached" });
      assert.equal(await images.count(), 0, "blurring a card with no pointer hover stops playback");
      await first.focus();
      await loaded("knowledge");
      await first.locator(".np-research-tool__copy").hover();
      const hoveredImage = await images.elementHandle();
      await first.evaluate(button => button.blur());
      assert.ok(await images.evaluate((image, original) => image === original, hoveredImage), "blurring a still-hovered card preserves its existing preview");
      assert.equal(await images.isVisible(), true);
      await hoveredImage.dispose();
      await page.mouse.move(0, 0);
      await images.waitFor({ state: "detached" });
      await group("discover").locator(".np-research-tools__expand:not(.np-research-tools__collapse)").click();
      await page.mouse.move(0, 0);
      await group("discover").getByRole("button", { name: "数据库查询", exact: true }).focus();
      await loaded("databaseQuery");
      await group("discover").locator(".np-research-tools__expand:not(.np-research-tools__collapse)").evaluate(button => button.click());
      await images.waitFor({ state: "detached" });
      assert.equal(await images.count(), 0, "collapsing the selected card's group stops playback");
      await page.mouse.move(0, 0);
      await page.setViewportSize({ width: 1440, height: 1050 });
      await page.locator('[data-research-columns="3"]').waitFor();
      await group("discover").getByRole("button", { name: "单体逆合成反推", exact: true }).focus();
      await loaded("monomerRetrosynthesis");
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator('[data-research-columns="1"]').waitFor();
      await images.waitFor({ state: "detached" });
      assert.equal(await images.count(), 0, "narrowing the viewport until the selected card is hidden stops playback");
      await page.setViewportSize(viewport);
      await page.locator(`[data-research-columns="${viewport.width === 1440 ? 3 : 1}"]`).waitFor();
      await group("optimize").getByRole("button", { name: "Tg 逆向设计", exact: true }).focus();
      await loaded("reverseDesign");
      await page.evaluate(() => {
        Object.defineProperty(document, "hidden", { configurable: true, value: true });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await images.waitFor({ state: "detached" });
      assert.equal(await images.count(), 0, "hiding the page stops playback");
      await page.evaluate(() => {
        delete document.hidden;
        delete document.visibilityState;
        document.dispatchEvent(new Event("visibilitychange"));
      });
      assert.equal(await images.count(), 0, "returning to the page does not restart playback");
      assert.deepEqual([...new Set(gifRequests)].sort(), ["knowledge", "homopolymerPrediction", "databaseQuery", "monomerRetrosynthesis", "reverseDesign"]
        .map(id => `/previews/research-tools/${id}.gif`).sort(), "only hovered or focused cards request GIF assets");
      await first.focus();
      await loaded("knowledge");
      await page.keyboard.press("Enter");
      await settled("knowledge");
      assert.equal(new URL(page.url()).pathname, "/knowledge", "the whole-card entry opens the original module with Enter");
      assert.equal(await images.count(), 0, "opening a module removes the selected preview");
      await directory();
      assert.equal(await images.count(), 0, "returning from a module leaves playback stopped");
      return { defaultGifRequests: 0, hoverAndFocusOnly: true, secondHoverStopsFirst: true, internalMovementKeepsPlayback: true,
        mouseLeaveStopsUnlessFocused: true, blurStopsUnlessHovered: true, collapseStopsPlayback: true, resizeHiddenCardStopsPlayback: true,
        pageHiddenStopsPlayback: true, keyboardFocusAndEntry: true, gifRequests: [...gifRequests], gifResponses: await Promise.all(gifResponses), screenshot };
    }
    async function verifyVisualRefinement() {
      const initialGifRequests = gifRequests.length;
      await directory();
      const toolsTitle = await titleStyle(".np-research-tools__intro h1");
      assert.equal(toolsTitle.text, "实验工具");
      assert.equal(toolsTitle.fontSize, "24px");
      assert.equal(toolsTitle.lineHeight, "32px");
      const previews = await page.locator(".np-research-tool").evaluateAll(cards => cards.map(card => {
        const thumbnail = card.querySelector(".np-research-tool__thumbnail");
        const cardHeight = card.getBoundingClientRect().height;
        const thumbnailHeight = thumbnail.getBoundingClientRect().height;
        const gifImages = [...card.querySelectorAll("img")].filter(image => /\.gif(?:[?#]|$)/i.test(image.currentSrc || image.src));
        return { module: card.querySelector("button[data-module-id]")?.dataset.moduleId ?? card.dataset.moduleId, cardHeight, thumbnailHeight, ratio: thumbnailHeight / cardHeight,
          entryButtons: card.querySelectorAll("button[data-module-id]").length, totalButtons: card.querySelectorAll("button").length,
          thumbnailIsButton: thumbnail.tagName === "BUTTON",
          scientificPreviews: thumbnail.querySelectorAll("svg.np-research-tool__science").length,
          videos: card.querySelectorAll("video").length, gifImages: gifImages.length,
          animatedSvgElements: thumbnail.querySelectorAll("animate, animateTransform, animateMotion").length };
      }));
      assert.ok(previews.length > 0);
      for (const preview of previews) {
        assert.ok(preview.ratio >= 0.60, `${preview.module}: thumbnail/card ratio ${preview.ratio} >= 0.60`);
        assert.ok(preview.cardHeight <= 250, `${preview.module}: card height ${preview.cardHeight} <= 250px`);
        assert.equal(preview.scientificPreviews, 1, `${preview.module}: one scientific SVG preview per card`);
        assert.equal(preview.entryButtons, 1, `${preview.module}: one whole-card entry`);
        assert.equal(preview.totalButtons, 1, `${preview.module}: no separate preview controls`);
        assert.equal(preview.thumbnailIsButton, false, `${preview.module}: the preview is part of the whole-card entry`);
        assert.equal(preview.videos + preview.gifImages + preview.animatedSvgElements, 0, `${preview.module}: default preview stays static until hovered or focused`);
      }
      assert.equal(gifRequests.length, initialGifRequests, "returning to the directory does not request GIFs");
      await page.locator("main").evaluate(element => { element.scrollTop = 0; });
      await page.mouse.move(0, 0);
      const toolsScreenshot = `research-tools-refined-${viewport.width}.png`;
      await page.screenshot({ path: resolve(output, toolsScreenshot) });
      const nav = await sidebar();
      const overviewAvailable = (await nav.locator('[data-module-id="overview"]').count()) > 0;
      if (!overviewAvailable) {
        if (viewport.width < 1024) await page.getByRole("button", { name: "关闭导航", exact: true }).click();
        return { toolsTitle, previews, toolsScreenshot, overview: { skipped: "Overview is not registered in this source." } };
      }
      await nav.locator('[data-module-id="overview"]').click();
      await settled("overview");
      const overviewTitle = await titleStyle(".ov-page .np-module-page-title");
      assert.equal(overviewTitle.text, "系统总览");
      assert.equal(overviewTitle.fontSize, toolsTitle.fontSize);
      assert.equal(overviewTitle.lineHeight, toolsTitle.lineHeight);
      await assertNoOverflow();
      const overviewScreenshot = `overview-refined-${viewport.width}.png`;
      await page.screenshot({ path: resolve(output, overviewScreenshot) });
      return { toolsTitle, previews, toolsScreenshot, overview: { title: overviewTitle, screenshot: overviewScreenshot } };
    }
    try {
      await page.mouse.move(0, 0);
      await page.goto(base+"/research-tools");
      await settled("researchTools");
      await group("discover").waitFor();
      await page.getByRole("heading", { name: "实验工具", exact: true }).waitFor();
      assert.deepEqual(gifRequests, [], "initial directory load does not request GIFs before hover or focus");
      assert.equal(await page.locator(".np-research-tool__thumbnail img").count(), 0);
      const expected=viewport.width>=1920 ? [4,4,2] : viewport.width===1440 ? [3,3,2] : [1,1,1];
      assert.deepEqual(await counts(),expected);
      assert.ok(!scripts.some(path=>/\/(PolymerMdSimulationPage|MonomerDftPage|ReverseDesignPage|HomopolymerPropertyPredictionPage)\./.test(path)), "directory does not load compute pages");
      assert.ok(!reads.some(path=>/\/(monomer-md|monomer-dft|polymer-md)\//.test(path)), "directory does not read computation endpoints");
      await assertNoOverflow();
      await group("discover").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').click();
      assert.deepEqual(await counts(),[7,expected[1],expected[2]]);
      await group("build").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').click();
      assert.deepEqual(await counts(),[7,6,expected[2]]);
      await group("discover").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').click();
      assert.deepEqual(await counts(),[expected[0],6,expected[2]]);
      await group("build").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').click();
      await page.locator("main").evaluate(el=>{ el.scrollTop=0; });
      await page.mouse.move(0,0);
      await page.screenshot({path:resolve(output,`research-tools-${viewport.width}.png`)});
      const nav=await sidebar();
      assert.equal(await nav.getByRole("button",{name:"材料发现 Discover",exact:true}).count(),0);
      const data=await nav.getByRole("button",{name:"数据管理 Data",exact:true}).boundingBox();
      const tools=await nav.locator('[data-module-id="researchTools"]').boundingBox();
      const assistant=await nav.locator('[data-module-id="home"]').boundingBox();
      assert.ok(data && tools && assistant && tools.y < data.y && assistant.y >= data.y+data.height);
      if (viewport.width < 1024) { await page.getByRole("button",{name:"关闭导航",exact:true}).click(); await page.getByRole("dialog",{name:"平台导航"}).waitFor({state:"hidden"}); }
      const selectionPlayback = [1440, 390].includes(viewport.width) ? await verifySelectionPlayback() : undefined;
      if (viewport.width === 1440) {
        for (const [index,[module,path]] of modules.entries()) {
          if (module !== "knowledge") await directory();
          const card=page.locator(`main button[data-module-id="${module}"]`);
          if (!(await card.isVisible())) await group(index < 7 ? "discover" : index < 13 ? "build" : "optimize").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').click();
          await card.click(); await settled(module);
          assert.equal(new URL(page.url()).pathname,path,module);
          assert.equal(await (await sidebar()).locator('[data-module-id="researchTools"]').getAttribute("aria-current"),"page");
        }
        await directory();
        assert.equal(await group("discover").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').getAttribute("aria-expanded"),"true");
        await page.mouse.move(0, 0);
        await page.reload(); await group("discover").waitFor();
        assert.deepEqual(await counts(),[3,3,2]);
        await group("discover").locator('button[data-module-id="knowledge"]').click(); await settled("knowledge");
        await page.goBack(); await settled("researchTools");
        await page.goForward(); await settled("knowledge");
        const finalNav=await sidebar();
        if (await finalNav.locator('[data-module-id="overview"]').count()) {
          await finalNav.locator('[data-module-id="overview"]').click(); await settled("overview");
        }
        await (await sidebar()).locator('[data-module-id="home"]').click(); await settled("home");
        assert.equal(new URL(page.url()).pathname,"/");
        if (expectedWorkspace) {
          const workspace=page.locator('iframe[title="智聚万物智能体工作台"]');
          await workspace.waitFor();
          assert.equal(await workspace.getAttribute("src"),expectedWorkspace);
        }
      } else if (viewport.width===390) {
        await group("optimize").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').click();
        await group("optimize").locator('[data-module-id="highThroughputWorkflowDemo"]').click();
        await settled("highThroughputWorkflowDemo");
        await directory();
        assert.equal(await group("optimize").locator('.np-research-tools__expand:not(.np-research-tools__collapse)').getAttribute("aria-expanded"),"true");
        await page.mouse.move(0, 0);
        await page.reload(); await group("optimize").waitFor();
        assert.deepEqual(await counts(),[1,1,1]);
      }
      const refinement = verifyRefinement ? await verifyVisualRefinement() : undefined;
      await assertNoOverflow();
      assert.deepEqual(blockedWrites,[]);
      assert.deepEqual(errors,[]);
      report.cases.push({viewport,defaultCounts:expected,independentExpansion:true,noOverflow:true,
        checkedModuleRoutes:viewport.width===1440?15:viewport.width===390?1:0,selectionPlayback,refinement,blockedWrites,errors});
    } catch(error) {
      await page.screenshot({path:resolve(output,`failure-${viewport.width}.png`)});
      await writeFile(resolve(output,`failure-${viewport.width}.json`),JSON.stringify({error:error.message,errors,blockedWrites},null,2));
      throw error;
    } finally { await context.close(); }
  }
  const touchContext = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const touchGifRequests = [], touchWrites = [], touchErrors = [];
  await touchContext.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (!["GET", "HEAD"].includes(request.method())) { touchWrites.push(request.method()+" "+url.pathname); return route.abort(); }
    if (request.isNavigationRequest() && request.frame().parentFrame()) return route.fulfill({ contentType: "text/html", body: "<!doctype html><html lang=zh><body>智能助手工作空间</body></html>" });
    if (url.origin !== base) return route.abort();
    if (url.pathname.startsWith("/api/")) return route.fulfill({ status: url.pathname === "/api/v1/auth/session" ? 200 : 503,
      json: url.pathname === "/api/v1/auth/session" ? identity : { detail: "Read-only interface fixture" } });
    if (/^\/previews\/research-tools\/[^/]+\.gif$/.test(url.pathname)) touchGifRequests.push(url.pathname);
    return route.continue();
  });
  const touchPage = await touchContext.newPage();
  touchPage.on("pageerror", error => touchErrors.push(error.message));
  try {
    await touchPage.goto(base+"/research-tools");
    await touchPage.locator('[data-module-content="researchTools"][data-module-phase="idle"]:not([inert])').waitFor();
    const touchEntry = touchPage.locator('[data-research-group="discover"] button[data-module-id="knowledge"]');
    await touchEntry.waitFor();
    assert.deepEqual(touchGifRequests, [], "a true touch context does not preload GIFs");
    await touchEntry.tap();
    await touchPage.locator('[data-module-content="knowledge"][data-module-phase="idle"]:not([inert])').waitFor();
    assert.equal(new URL(touchPage.url()).pathname, "/knowledge", "a touch tap directly opens the original module");
    assert.deepEqual(touchGifRequests, [], "touch pointerenter and focus do not start a preview before navigation");
    assert.deepEqual(touchWrites, []);
    assert.deepEqual(touchErrors, []);
    report.touchCase = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, tapOpensModule: true, gifRequests: touchGifRequests, blockedWrites: touchWrites, errors: touchErrors };
  } catch (error) {
    await touchPage.screenshot({ path: resolve(output, "failure-touch-390.png") });
    throw error;
  } finally { await touchContext.close(); }
  report.passed=true;
  await writeFile(resolve(output,"research-tools-results.json"),JSON.stringify(report,null,2)+"\n");
  console.log(JSON.stringify(report));
} finally { await browser.close(); }
