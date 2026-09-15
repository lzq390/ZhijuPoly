import { pollBrowser } from "./browser-poll.mjs";
import { verifyKetcherNativeMenu } from "./verify-ketcher-native-menu.mjs";
// Real editor/browser verification. Business requests are fixtures; no jobs or records are submitted.
import assert from "node:assert/strict";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { resolve } from "node:path";

const { chromium } = await import(process.env.STRUCTURE_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.STRUCTURE_BASE_URL || "http://127.0.0.1:5187";
assert.equal(new URL(base).hostname, "127.0.0.1");
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || "/tmp/nexpoly-structure-browser");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ["--no-sandbox", "--enable-unsafe-swiftshader"] });
const reducedMotion = process.env.STRUCTURE_REDUCED_MOTION || "reduce";
assert.ok(["reduce", "no-preference"].includes(reducedMotion));
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion });
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
const page = await context.newPage();
const errors = [];
const requests3D = [];
const resources = [];
const missingResources = [];
const renderWarnings = [];
let molfile3D = "";
page.on("pageerror", (error) => errors.push(error.message));
page.on("console", message => { if (/Cannot update a component|Cannot update .*while rendering|unmounted root/.test(message.text())) renderWarnings.push(message.text()); });
context.on("request", request => {
  if (/ketcher|Ketcher|\/assets\/|\.css(?:\?|$)/.test(request.url())) resources.push(request.url());
});
context.on("response", response => {
  const url = new URL(response.url());
  if (response.status() >= 400 && !url.pathname.startsWith('/api/') &&
      ['script', 'stylesheet', 'font', 'image'].includes(response.request().resourceType())) {
    missingResources.push({ url: response.url(), status: response.status() });
  }
});
await context.route("**/*", (route) => {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin !== new URL(base).origin && !["blob:", "data:"].includes(url.protocol)) return route.abort();
  if (url.pathname.endsWith("/structure/standardize-smiles")) {
    const { smiles } = request.postDataJSON();
    return smiles.includes("(")
      ? route.fulfill({ status: 422, json: { detail: "Invalid test SMILES" } })
      : route.fulfill({ json: { input_smiles: smiles, standardized_smiles: smiles } });
  }
  if (url.pathname.endsWith("/structure/3d")) {
    const { smiles } = request.postDataJSON();
    requests3D.push(smiles);
    return route.fulfill({ json: { molblock: molfile3D, capped_smiles: smiles, format: "mol" } });
  }
  if (url.pathname.startsWith("/api/")) return route.fulfill({ status: 503, json: { detail: "Service disabled by browser fixture" } });
  return ["GET", "HEAD"].includes(request.method()) ? route.continue() : route.abort();
});
await context.addInitScript(() => {
  // SDK access is isolated in this test helper; application consumers use the adapter.
  window.__probeEditor = () => {
    const root = document.querySelector("[data-structure-editor]");
    return root?.dataset.editorEngine === "iframe" ? root.querySelector("iframe")?.contentWindow?.ketcher : window.ketcher;
  };
});
const patches = JSON.parse(await readFile(new URL('../patches/ketcher-manifest.json', import.meta.url), 'utf8'));
const result = { reducedMotion, patch: patches.revision, patchHashes: patches.patches,
  lockSha256: createHash('sha256').update(await readFile(new URL('../package-lock.json', import.meta.url))).digest('hex'),
  paths: [], viewports: [], errors, renderWarnings, requests3D, missingResources };
async function ready() {
  await page.locator('[data-structure-editor][data-editor-status="ready"]').waitFor({ timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('[data-module-content]')?.dataset.modulePhase === 'idle');
  assert.equal(await page.locator("[data-structure-editor]").count(), 1);
}
async function navigate(path) {
  const previous = await page.locator("[data-structure-editor]").elementHandle();
  await page.evaluate((path) => { history.pushState({}, "", path); dispatchEvent(new PopStateEvent("popstate")); }, path);
  if (previous) await page.waitForFunction((node) => !node.isConnected, previous);
  await previous?.dispose();
  await ready();
  result.paths.push(path);
}
async function read() {
  return page.evaluate(async () => {
    const editor = window.__probeEditor();
    const ket = JSON.parse(await editor.getKet());
    const molecule = Object.values(ket).find((node) => node?.type === "molecule" && node.atoms?.length);
    const atoms = molecule?.atoms || [];
    const origin = atoms[0]?.location || [0, 0];
    return { smiles: await editor.getSmiles(), ket,
      geometry: atoms.map((atom) => [atom.label, ...atom.location.slice(0, 2).map((value, i) => Math.round((value - origin[i]) * 1000) / 1000)]),
      annotations: ket.root.nodes.filter((node) => node.type === "text").map((node) => ({
        text: JSON.parse(node.data.content).blocks.map((block) => block.text).join("\n"),
        x: node.data.position.x - origin[0], y: node.data.position.y - origin[1]
      })) };
  });
}
try {
  const start = Date.now();
  await page.goto(`${base}/structure-workbench`, { waitUntil: "domcontentloaded" });
  await ready();
  result.readyMs = Date.now() - start;
  result.engine = await page.locator('[data-structure-editor]').getAttribute('data-editor-engine');
  result.sdk = result.engine === 'react' ? '3.8.0' : '3.7.0';
  if (result.engine === 'react') result.nativeSettingsMenu = await verifyKetcherNativeMenu(page);
  const input = () => page.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" });
  await input().fill("CCO");
  // Observe the public commit state before querying the SDK. The legacy 3.7
  // Worker cannot isolate a raw probe export from an in-progress import.
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await pollBrowser(page, async () => (await window.__probeEditor().getSmiles()) === "CCO");
  const beforeDrag = (await read()).geometry;
  const oxygen = await page.evaluate(() => {
    const editor = window.__probeEditor();
    editor.editor.selection(null);
    const atom = [...editor.editor.render.paper.canvas.querySelectorAll('text')].find(node => node.textContent === 'O').getBoundingClientRect();
    const iframe = document.querySelector('[data-structure-editor] iframe');
    const frame = iframe?.getBoundingClientRect();
    const scale = iframe ? frame.width / iframe.contentWindow.innerWidth : 1;
    return { x: (frame?.x || 0) + (atom.x + atom.width / 2) * scale,
      y: (frame?.y || 0) + (atom.y + atom.height / 2) * scale };
  });
  await page.mouse.move(oxygen.x, oxygen.y);
  await page.mouse.down();
  await page.mouse.move(oxygen.x + 80, oxygen.y - 35, { steps: 8 });
  await page.mouse.up();
  await pollBrowser(page, async before => {
    const ket = JSON.parse(await window.__probeEditor().getKet());
    const atoms = Object.values(ket).find(node => node?.type === 'molecule').atoms;
    const origin = atoms[0].location;
    const geometry = atoms.map(atom => [atom.label, ...atom.location.slice(0, 2).map((value, i) => Math.round((value - origin[i]) * 1000) / 1000)]);
    return JSON.stringify(geometry) !== JSON.stringify(before);
  }, beforeDrag);
  result.realAtomDrag = true;
  const editorUI = await page.locator("[data-structure-editor]").getAttribute("data-editor-engine") === "iframe"
    ? page.frameLocator("[data-structure-editor] iframe") : page;
  await editorUI.getByTestId("text").click();
  const editorBox = await page.locator("[data-structure-editor]").boundingBox();
  await page.mouse.click(editorBox.x + editorBox.width * .55, editorBox.y + editorBox.height * .3);
  await editorUI.getByTestId("text-editor").fill("NexPoly layout label");
  await editorUI.getByRole("button", { name: "Apply", exact: true }).click();
  await pollBrowser(page, async () => (await window.__probeEditor().getKet()).includes("NexPoly layout label"));
  const expected = await read();
  for (const path of ["/database-query", "/explorer", "/homopolymer-property-prediction", "/conditional-generation", "/reverse-design", "/structure-workbench"]) {
    await navigate(path);
    const actual = await read();
    assert.equal(actual.smiles, "CCO", path);
    assert.deepEqual(actual.geometry, expected.geometry, `Layout changed on ${path}`);
    assert.ok(JSON.stringify(actual.ket).includes("NexPoly layout label"), `Label lost on ${path}`);
    assert.equal(actual.annotations.length, expected.annotations.length);
    actual.annotations.forEach((label, index) => {
      const previous = expected.annotations[index];
      assert.equal(label.text, previous.text);
      assert.ok(Math.abs(label.x - previous.x) < .01 && Math.abs(label.y - previous.y) < .01, `Label moved on ${path}`);
    });
  }
  result.layoutRoundTrip = true;
  result.annotationRoundTrip = true;

  await page.getByRole("button", { name: "生成SMILES", exact: true }).click();
  await page.getByText("SMILES 与画板布局已同步。", { exact: true }).waitFor();
  result.explicitSync = true;
  molfile3D = await page.evaluate(() => window.__probeEditor().getMolfile("v2000"));
  const response3D = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/structure/3d"));
  await page.getByRole("button", { name: "3D构象", exact: true }).click();
  await response3D;
  await page.waitForFunction(() => document.querySelector('[data-workbench-tool="3d"]')?.textContent.includes("2D"));
  await page.getByRole("button", { name: "2D画布", exact: true }).click();
  assert.ok(requests3D.includes("CCO"));
  assert.deepEqual((await read()).geometry, expected.geometry);
  result.external3D = true;

  await page.locator('input[type="file"]').first().setInputFiles({ name: "recognition-fixture.png", mimeType: "image/png",
    buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9YQAAAAASUVORK5CYII=", "base64") });
  await page.getByText(/已恢复原画布。/).waitFor();
  assert.equal((await read()).smiles, "CCO");
  assert.deepEqual((await read()).geometry, expected.geometry);
  assert.ok(JSON.stringify((await read()).ket).includes("NexPoly layout label"));
  result.failedImageRollback = true;

  await input().fill("CC(");
  await page.getByText("SMILES 无效或尚未完整，原画板未修改。", { exact: true }).waitFor();
  await navigate("/database-query");
  assert.equal(await input().inputValue(), "CC(");
  assert.equal((await read()).smiles, "CCO");
  result.invalidDraftRetained = true;

  // A real unsaved edit requires export; a clean document now reuses its saved
  // snapshot. Block KET first so autosave cannot accept the new edit earlier.
  await page.evaluate(async () => {
    const editor = window.__probeEditor();
    editor.getKet = () => new Promise(() => {});
    await editor.setMolecule('CCN');
  });
  await navigate("/explorer");
  await page.getByRole("status").filter({ hasText: "上一次保存" }).waitFor();
  assert.equal((await read()).smiles, "CCO");
  result.navigationFallback = true;
  await page.getByRole("button", { name: "关闭画板同步提示" }).click();

  await page.getByRole("button", { name: "清空画布", exact: true }).click();
  await pollBrowser(page, async () => (await window.__probeEditor().getSmiles()) === "");
  await navigate("/structure-workbench");
  assert.equal((await read()).smiles, "");
  assert.equal(await input().inputValue(), "");
  result.emptyRoundTrip = true;

  await input().fill("*CC*");
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await navigate("/database-query");
  assert.equal(await input().inputValue(), "*CC*");
  result.polymerEndGroups = true;
  const png = await page.evaluate(async () => {
    const editor = window.__probeEditor();
    const options = { outputFormat: "png", backgroundColor: "255, 255, 255", "image-resolution": 144 };
    let blob;
    try { blob = await editor.generateImage(await editor.getKet(), options); }
    catch { blob = await editor.generateImage(await editor.getMolfile('v2000'), options); }
    return { size: blob.size, signature: Array.from(new Uint8Array(await blob.arrayBuffer()).slice(0, 8)) };
  });
  assert.ok(png.size > 0);
  assert.deepEqual(png.signature, [137, 80, 78, 71, 13, 10, 26, 10]);
  result.png = png;

  // DFT uses the shared document without owning a canvas. Return immediately
  // while its text validation may still be pending in the App-owned store.
  await page.evaluate(() => { history.pushState({}, "", "/monomer-dft"); dispatchEvent(new PopStateEvent("popstate")); });
  // fill() does not reliably wait for an inert ancestor. A user can type only
  // after the DFT page's transition unlocks, even though its input exists sooner.
  await page.waitForFunction(() => document.querySelector('[data-module-content="monomerDft"]')?.dataset.modulePhase === 'idle');
  await page.locator("#monomer-dft-smiles").fill("CCN");
  assert.equal(await page.locator("#monomer-dft-smiles").inputValue(), "CCN");
  await page.evaluate(() => { history.pushState({}, "", "/structure-workbench"); dispatchEvent(new PopStateEvent("popstate")); });
  await ready();
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await pollBrowser(page, async () => (await window.__probeEditor()?.getSmiles()) === "CCN");
  assert.equal(await input().inputValue(), "CCN");
  assert.ok(!JSON.stringify((await read()).ket).includes("NexPoly layout label"));
  result.externalDftWrite = true;

  if (result.engine === 'react') {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Performance.enable');
    await page.evaluate(() => { window.__retiredCanvasEditors = []; });
    result.lifecycle = [];
    for (let cycle = 0; cycle <= 10; ++cycle) {
      await page.evaluate(() => window.__retiredCanvasEditors.push(new WeakRef(window.__probeEditor())));
      await navigate(cycle % 2 ? '/structure-workbench' : '/database-query');
      await page.waitForTimeout(500);
      await cdp.send('Runtime.discardConsoleEntries');
      await cdp.send('HeapProfiler.collectGarbage');
      const metrics = (await cdp.send('Performance.getMetrics')).metrics;
      const sample = { cycle, workers: page.workers().length,
        ...Object.fromEntries(metrics.filter(m => ['JSHeapUsedSize','Nodes','JSEventListeners'].includes(m.name)).map(m => [m.name,m.value])),
        retainedEditors: await page.evaluate(() => window.__retiredCanvasEditors.filter(ref => ref.deref()).length) };
      result.lifecycle.push(sample);
      assert.equal(sample.workers, 1);
      assert.equal(sample.retainedEditors, 0, 'A retired application editor is still reachable');
    }
    const first = result.lifecycle[0], last = result.lifecycle.at(-1);
    assert.ok(last.JSEventListeners <= first.JSEventListeners + 5, 'Application editor listeners grow on remount');
    await navigate('/structure-workbench');
    await cdp.detach();
  }

  for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 768 }, { width: 2560, height: 1440 }, { width: 1440, height: 900 }]) {
    await page.setViewportSize(viewport);
    await ready();
    // The SDK throttles its resize observer; measure the settled canvas.
    await page.waitForTimeout(600);
    await page.waitForFunction(() => {
      const frame = document.querySelector("[data-structure-editor] iframe");
      const win = frame?.contentWindow || window;
      const paper = window.__probeEditor()?.editor?.render?.paper?.canvas;
      return paper && [...paper.querySelectorAll("text")].some((text) => {
        const box = text.getBoundingClientRect();
        return text.textContent.includes("N") && box.width > 0 && box.left >= 40 && box.right < win.innerWidth - 20 && box.top >= 40 && box.bottom < win.innerHeight - 30;
      });
    });
    const box = await page.locator("[data-structure-editor]").boundingBox();
    assert.ok(box.width > 0 && box.height >= 300);
    assert.ok(box.x >= -1 && box.x + box.width <= viewport.width + 1);
    const point = await page.evaluate(() => {
      const paper = window.__probeEditor().editor.render.paper.canvas;
      const text = [...paper.querySelectorAll('text')].find(node => node.textContent === 'N');
      const atom = text.getBoundingClientRect();
      const iframe = document.querySelector('[data-structure-editor] iframe');
      const frame = iframe?.getBoundingClientRect();
      const scale = iframe ? frame.width / iframe.contentWindow.innerWidth : 1;
      return { x: (frame?.x || 0) + (atom.x + atom.width / 2) * scale,
        y: (frame?.y || 0) + (atom.y + atom.height / 2) * scale };
    });
    const ui = result.engine === 'iframe' ? page.frameLocator('[data-structure-editor] iframe') : page;
    await ui.locator('[data-testid="select-rectangle"]:visible').click();
    await page.mouse.click(point.x, point.y);
    await page.waitForFunction(() => window.__probeEditor().editor.selection()?.atoms?.length > 0);
    // Use the real pointer hit result to open an SDK popup, then close it.
    await page.mouse.click(point.x, point.y, { button: 'right' });
    await ui.getByTestId('Edit...-option').waitFor();
    await page.keyboard.press('Escape');
    await ui.getByTestId('Edit...-option').waitFor({ state: 'hidden' });
    if (viewport.width === 1440) {
      // Closing a popup can return focus to the host document. Explicitly
      // activate the canvas before exercising its scoped keyboard commands.
      await page.mouse.click(point.x, point.y);
      await page.evaluate(() => navigator.clipboard.writeText(''));
      await page.keyboard.press('Control+a');
      await page.waitForFunction(() => window.__probeEditor().editor.selection()?.atoms?.length === 3);
      // Selection changes before the SDK's debounced command state. A copy
      // shortcut sent while its action is disabled is prevented by the SDK.
      await ui.locator('[data-testid="copy-button"]:enabled').waitFor({ state: 'visible' });
      // Observe SDK completion before querying the OS clipboard. Reading it
      // while the SDK is still exporting creates a second asynchronous client.
      await page.evaluate(() => {
        window.__probeCopyComplete = false;
        const win = document.querySelector('[data-structure-editor] iframe')?.contentWindow || window;
        win.addEventListener('copyOrCutComplete', () => { window.__probeCopyComplete = true; }, { once: true });
      });
      await page.keyboard.press('Control+c');
      const copied = await page.waitForFunction(() => window.__probeCopyComplete, undefined, { timeout: 8000 })
        .then(() => page.evaluate(() => navigator.clipboard.readText())).catch(async error => {
        result.clipboardFailure = await page.evaluate(async () => ({
          active: document.activeElement?.outerHTML,
          selection: window.__probeEditor().editor.selection(),
          clipboard: await navigator.clipboard.readText(),
          focused: document.hasFocus()
        }));
        throw error;
      });
      assert.match(copied, /V2000|V3000|CCN/);
      await page.evaluate(() => window.__probeEditor().setMolecule(''));
      await page.mouse.click(point.x, point.y);
      await page.keyboard.press('Control+v');
      await page.waitForFunction(() => Boolean(window.__probeEditor().editor._tool?.struct?.atoms));
      await page.mouse.click(point.x, point.y);
      await page.waitForFunction(() => window.__probeEditor().editor.struct().atoms.size === 3);
      await pollBrowser(page, async () => (await window.__probeEditor().getSmiles()) === 'CCN');
      result.clipboardShortcuts = true;
    }
    result.viewports.push({ ...viewport, editor: box, atomVisible: true, atomHit: true, contextMenu: true });
    await page.screenshot({ path: resolve(output, `${viewport.width}.png`) });
  }
  result.deepLinks = [];
  for (const path of ["/structure-workbench", "/database-query", "/explorer", "/homopolymer-property-prediction", "/conditional-generation", "/reverse-design"]) {
    const direct = await context.newPage();
    const diagnostic = [];
    direct.on("pageerror", (error) => errors.push(error.message));
    direct.on('console', message => { if (message.type() === 'error') diagnostic.push(message.text()); });
    try {
      await direct.goto(`${base}${path}`, { waitUntil: "domcontentloaded" });
      await direct.locator('[data-structure-editor][data-editor-status="ready"]').waitFor({ timeout: 30000 });
      assert.equal(await direct.locator("[data-structure-editor]").count(), 1);
      result.deepLinks.push(path);
    } catch (error) {
      result.failedDeepLink = { path, url: direct.url(), console: diagnostic, ...await direct.evaluate(() => {
        const editor = document.querySelector('[data-structure-editor]');
        return { state: editor?.getAttribute('data-editor-status'), text: editor?.textContent || document.body.innerText };
      }) };
      await direct.screenshot({ path: resolve(output, 'failed-deep-link.png') });
      throw error;
    } finally { await direct.close(); }
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(renderWarnings, []);
  assert.deepEqual(missingResources, []);
  result.editorResources = [...new Set(resources.filter(url => /ketcher|Ketcher/.test(url)))];
  if (result.engine === 'react') assert.ok(!resources.some(url => new URL(url).pathname.startsWith('/ketcher/')), 'React requested the legacy static application');
  else assert.ok(!resources.some(url => /KetcherReactRuntime|ketcher-native\.css|\/deps\/ketcher/.test(url)), 'Iframe loaded the native SDK');
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.failure = error.stack;
  await page.screenshot({ path: resolve(output, "failure.png") });
  process.exitCode = 1;
} finally {
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}
