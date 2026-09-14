// Verify the local S1 demo without uploading files or submitting backend jobs.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const { chromium } = await import(process.env.PAGE_PLAYWRIGHT_MODULE || "playwright");
const base = new URL(process.env.PAGE_BASE_URL || "http://127.0.0.1:9001");
assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(base.hostname), "Use a local frontend URL");
assert.ok(["http:", "https:"].includes(base.protocol));
const output = resolve(process.env.PAGE_ARTIFACT_DIR || "/tmp/nexpoly-high-throughput-states");
await mkdir(output, { recursive: true });
const viewports = [[390, 844], [1920, 1080], [2560, 1440]]
  .filter(([width]) => !process.env.PAGE_VIEWPORT || width === Number(process.env.PAGE_VIEWPORT));
assert.ok(viewports.length, "PAGE_VIEWPORT must select one of 390, 1920 or 2560");
const results = [];
const failures = [];
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PAGE_CHROMIUM_PATH || undefined,
  args: ["--no-sandbox"],
});

async function verifyUploadStates(page, viewport) {
  const [width, height] = viewport;
  await page.goto(new URL("/high-throughput-workflow-demo", base).href, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "确认场景设置，进入 S1", exact: true }).click();
  await page.getByRole("button", { name: "展开 Tg Agent", exact: true }).click();
  // S1 only compares the filename with its bundled demo samples. No API upload occurs.
  await page.getByLabel("上传 Tg CSV 文件", { exact: true }).setInputFiles({
    name: "wrong.csv", mimeType: "text/csv", buffer: Buffer.from("invalid"),
  });
  await page.locator(".ht-s1-agent-status.error").waitFor();
  await page.getByRole("button", { name: "重新上传 CSV（Tg）", exact: true }).focus();
  // Wait for the existing colour transitions to finish before reading computed styles.
  await page.waitForFunction(() => {
    const area = document.querySelector(".ht-s1-upload-area.error");
    const status = document.querySelector(".ht-s1-agent-status.error");
    return area && status
      && getComputedStyle(area).borderTopColor === "rgba(220, 38, 38, 0.24)"
      && getComputedStyle(status).color === "rgb(153, 27, 27)";
  });
  const error = await page.evaluate(() => {
    const style = selector => {
      const element = document.querySelector(selector);
      const computed = getComputedStyle(element);
      return { color: computed.color, border: computed.borderTopColor, background: computed.backgroundColor };
    };
    return {
      area: style(".ht-s1-upload-area.error"),
      summaryIcon: style(".ht-s1-upload-area.error .ht-s1-file-summary > svg"),
      status: style(".ht-s1-agent-status.error"),
      previewIcon: style(".ht-s1-preview-empty.error > svg"),
      focusedErrorArea: Boolean(document.activeElement?.closest(".ht-s1-upload-area.error")),
    };
  });
  assert.equal(error.focusedErrorArea, true);
  assert.equal(error.area.border, "rgba(220, 38, 38, 0.24)");
  assert.equal(error.area.background, "rgb(254, 242, 242)");
  assert.equal(error.summaryIcon.color, "rgb(220, 38, 38)");
  assert.equal(error.status.color, "rgb(153, 27, 27)");
  assert.deepEqual(error.previewIcon, {
    color: "rgb(220, 38, 38)", border: "rgba(220, 38, 38, 0.24)", background: "rgb(254, 242, 242)",
  });
  await page.screenshot({ path: resolve(output, `high-throughput-error-${width}-${height}.png`) });

  await page.getByRole("button", { name: "上传样例（Tg）", exact: true }).click();
  await page.waitForFunction(() => {
    const status = document.querySelector(".ht-s1-agent-status.loading");
    return status && getComputedStyle(status).color === "rgb(29, 78, 216)";
  });
  const loading = await page.locator(".ht-s1-agent-status.loading").evaluate(element => getComputedStyle(element).color);
  assert.equal(loading, "rgb(29, 78, 216)");
  await page.waitForFunction(() => {
    const status = document.querySelector(".ht-s1-agent-status.ready");
    return status && getComputedStyle(status).color === "rgb(4, 120, 87)";
  });
  const ready = await page.locator(".ht-s1-agent-status.ready").evaluate(element => getComputedStyle(element).color);
  assert.equal(ready, "rgb(4, 120, 87)");
  assert.equal(await page.locator(".ht-s1-preview-empty.error").count(), 0);
  await page.getByRole("region", { name: "Tg 先验数据表，可滚动", exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, `high-throughput-recovered-${width}-${height}.png`) });
  return { viewport, error, loading, ready };
}

try {
  for (const viewport of viewports) {
    const [width, height] = viewport;
    const context = await browser.newContext({ viewport: { width, height }, reducedMotion: "reduce" });
    await context.route("**/*", route => (
      new URL(route.request().url()).origin === base.origin
      && ["GET", "HEAD"].includes(route.request().method())
        ? route.continue() : route.abort()
    ));
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on("pageerror", error => failures.push({ viewport, error: error.message }));
    try {
      results.push(await verifyUploadStates(page, viewport));
    } catch (error) {
      failures.push({ viewport, error: String(error) });
    } finally {
      await context.close();
    }
    console.log(`${width}×${height}: checked S1 upload states; ${failures.length} failures so far`);
  }
} finally {
  await browser.close();
  await writeFile(resolve(output, "report.json"), JSON.stringify({ results, failures }, null, 2));
}
assert.equal(failures.length, 0, JSON.stringify(failures, null, 2));
console.log(`Passed S1 upload error, focused error, loading and recovery at ${results.length} viewports. Artifacts: ${output}`);
