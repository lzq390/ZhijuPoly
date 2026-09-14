import { pollBrowser } from "./browser-poll.mjs";
// Real SDK + real assistant capture pipeline; all network business calls are local fixtures.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.STRUCTURE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:5189';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || '/tmp/nexpoly-structure-ai');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
const report = { requests: [], errors: [] };
page.on('pageerror', e => report.errors.push(e.stack || e.message));
await page.addInitScript(appOrigin => {
  // Playwright also runs init scripts in child/error documents; only seed the app page.
  if (window !== window.top || window.location.origin !== appOrigin) return;
  localStorage.setItem('nexpoly.assistant.tg.page-context-consent.v1', 'granted');
  window.__sdk = () => document.querySelector('[data-structure-editor] iframe')?.contentWindow?.ketcher || window.ketcher;
}, new URL(base).origin);
await page.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== new URL(base).origin && !['data:', 'blob:'].includes(url.protocol)) return route.abort();
  if (url.pathname.endsWith('/structure/standardize-smiles')) return route.fulfill({ json: { standardized_smiles: request.postDataJSON().smiles } });
  if (url.pathname.endsWith('/assistant/tg/status')) return route.fulfill({ json: { enabled: true, configured: true, image: { supported: true, max_files: 2, max_canvas_snapshots: 1, max_user_upload_files: 1, max_bytes: 5242880, max_total_bytes: 10485760, accepted_mime_types: ['image/png'] } } });
  if (url.pathname.endsWith('/assistant/tg/guide')) return route.fulfill({ json: { module: 'reverseDesign', version: 3, language: 'zh-CN', defaults: {}, sections: [] } });
  if (url.pathname.includes('/assistant/tg/chat/')) {
    let payload, png;
    if (url.pathname.endsWith('/image-stream')) {
      const form = await new Response(request.postDataBuffer(), { headers: { 'Content-Type': request.headers()['content-type'] } }).formData();
      payload = JSON.parse(form.get('payload'));
      const blob = form.get('canvas_image');
      if (blob) png = Buffer.from(await blob.arrayBuffer());
    } else payload = request.postDataJSON();
    const n = report.requests.length + 1;
    const row = { path: url.pathname, payload, pngBytes: png?.length || 0, pngSha256: png ? createHash('sha256').update(png).digest('hex') : null };
    if (png) {
      assert.deepEqual([...png.subarray(0, 8)], [137,80,78,71,13,10,26,10]);
      await writeFile(resolve(output, `canvas-${n}.png`), png);
      row.pixels = await page.evaluate(async data => {
        const blob = await (await fetch(data)).blob(); const bitmap = await createImageBitmap(blob);
        const canvas = document.createElement('canvas'); canvas.width = bitmap.width; canvas.height = bitmap.height;
        const context = canvas.getContext('2d'); context.drawImage(bitmap, 0, 0); bitmap.close();
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        let ink = 0; for (let i=0;i<pixels.length;i+=4) if (pixels[i+3]>0 && Math.min(...pixels.slice(i,i+3))<220) ++ink;
        return { width: canvas.width, height: canvas.height, ink };
      }, `data:image/png;base64,${png.toString('base64')}`);
      assert.ok(row.pixels.ink > 30, 'AI image has no visible structure');
    }
    report.requests.push(row);
    return route.fulfill({ contentType: 'text/event-stream', body: `event: meta\ndata: {"request_id":"fixture-${n}","context_trimmed":[],"context_attached":true}\n\nevent: token\ndata: {"content":"fixture-${n}"}\n\nevent: done\ndata: {"message":"fixture-${n}"}\n\n` });
  }
  if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 503, json: { detail: 'Fixture disabled service' } });
  return ['GET','HEAD'].includes(request.method()) ? route.continue() : route.abort();
});
async function send(message, n) {
  await page.getByRole('textbox', { name: '发送给 AI 助手的消息' }).fill(message);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await page.getByText(`fixture-${n}`, { exact: true }).waitFor();
  assert.equal(report.requests.length, n);
}
async function moveAtom() {
  await page.evaluate(async () => {
    const sdk = window.__sdk(); const ket = JSON.parse(await sdk.getKet());
    const mol = Object.values(ket).find(n => n?.type === 'molecule'); mol.atoms.at(-1).location[0] += 1.5;
    await sdk.setMolecule(JSON.stringify(ket));
  });
  await page.waitForTimeout(400);
}
try {
  await page.goto(`${base}/reverse-design`);
  await page.locator('[data-editor-status="ready"]').waitFor({ timeout: 30000 });
  report.engine = await page.locator('[data-structure-editor]').getAttribute('data-editor-engine');
  await page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' }).fill('CCO');
  await pollBrowser(page, async () => await window.__sdk().getSmiles() === 'CCO');
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await page.evaluate(() => {
    const sdk = window.__sdk(); window.__imageSources = []; window.__renderImage = sdk.generateImage.bind(sdk);
    sdk.generateImage = (...args) => { window.__imageSources.push(args[0]); return window.__renderImage(...args); };
  });
  await page.getByRole('button', { name: 'AI 助手', exact: true }).click();
  await page.getByRole('checkbox', { name: '附带当前页面' }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: '附带当前页面' }).isChecked(), true);
  await send('分析当前画板', 1);
  await send('再次读取相同画板', 2);
  assert.equal(await page.evaluate(() => window.__imageSources.length), 1, 'Unchanged canvas did not reuse the PNG cache');
  assert.equal(report.requests[0].pngSha256, report.requests[1].pngSha256);
  await moveAtom();
  await send('读取新的布局', 3);
  assert.equal(await page.evaluate(() => window.__imageSources.length), 2);
  assert.notEqual(report.requests[2].pngSha256, report.requests[1].pngSha256, 'Layout did not change the AI image');
  report.layoutCache = true;
  await moveAtom();
  await page.evaluate(() => { window.__sdk().generateImage = () => Promise.reject(new Error('Expected image failure')); });
  await send('生成失败时保留结构上下文', 4);
  assert.equal(report.requests[3].pngBytes, 0);
  assert.match(JSON.stringify(report.requests[3].payload), /CCO/);
  await page.locator('.tg-assistant-process').last().locator('summary').click();
  await page.getByText(/本轮已使用 SMILES 兜底/).waitFor();
  report.smilesFallback = true;
  await moveAtom();
  await page.evaluate(() => { window.__sdk().generateImage = () => new Promise(resolve => { window.__releaseImage = resolve; }); });
  await page.getByRole('textbox', { name: '发送给 AI 助手的消息' }).fill('取消尚未生成的画板截图');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await page.waitForFunction(() => Boolean(window.__releaseImage));
  await page.getByRole('button', { name: '停止生成', exact: true }).click();
  await page.getByRole('button', { name: '发送消息', exact: true }).waitFor();
  await page.evaluate(async () => { window.__releaseImage(await window.__renderImage(await window.__sdk().getKet(), { outputFormat: 'png' })); });
  await page.waitForTimeout(500);
  assert.equal(report.requests.length, 4, 'Cancelled capture still sent a request');
  report.cancelled = true;
  await page.evaluate(() => { window.__sdk().generateImage = window.__renderImage; });
  await page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' }).fill('*CC*');
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await send('读取聚合物端基画板图片', 5);
  assert.ok(report.requests[4].pngBytes > 100, 'Polymer PNG fallback failed');
  assert.equal(report.requests[4].payload.page_context.structure.smiles, '*CC*');
  report.polymerPng = true;
  assert.deepEqual(report.errors, []);
  report.passed = true;
} catch (error) { report.passed = false; report.failure = error.stack; await page.screenshot({path:resolve(output,'failure.png')}); }
finally { await writeFile(resolve(output,'result.json'), JSON.stringify(report,null,2)); console.log(JSON.stringify(report,null,2)); await browser.close(); if(!report.passed)process.exitCode=1; }
