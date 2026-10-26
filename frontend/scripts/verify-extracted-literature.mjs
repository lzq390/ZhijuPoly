// Browser verification with local route fixtures. Every API call is intercepted;
// no database, object storage, recording service, upload or model is contacted.
// All record identifiers and values below are invented UI examples, not extraction evidence.
// With Node 22, first run: npm run dev -- --host 127.0.0.1 --port 5186 --strictPort
// Then: node scripts/verify-extracted-literature.mjs
// PAGE_BASE_URL and PAGE_ARTIFACT_DIR optionally override the local URL/output.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.PAGE_BASE_URL || "http://127.0.0.1:5186";
assert.equal(new URL(base).hostname, "127.0.0.1");
const output = resolve(process.env.PAGE_ARTIFACT_DIR || "/tmp/nexpoly-extracted-literature");
await mkdir(output, { recursive: true });

const paper = {
  data_source: "extracted_literature", knowledge_id: null, document_uid: "ui-paper", generation_id: "ui-generation", snapshot_uid: "ui-snapshot",
  title: "聚酰亚胺样品的热性能与合成条件（界面验证样本）", authors: ["Synthetic Author A", "Synthetic Author B"], journal: "Synthetic UI Journal", year: 2021, doi: "10.5555/synthetic-ui-001",
  keywords: ["polyimide", "thermal properties"], abstract: "本地界面验证样本：按样品区分性质、测试条件与工艺，不触发真实检索或提取。",
  abstract_snippet: "polyimide 聚酰亚胺样品的性质、测试条件与合成信息。", matched_fields: ["title", "abstract"], matched_terms: ["polyimide"],
  counts: { samples: 2, properties: 2, processes: 1 }, capabilities: { has_properties: true, has_processes: true, has_original: true, has_fulltext: true, has_pdf: true }
};
const detail = {
  document: paper, generation_id: paper.generation_id, snapshot_uid: paper.snapshot_uid, capabilities: paper.capabilities, quality_flags: [],
  extraction: { source_paper_id: "900001", source_extract_id: "900000000000000001", model_version: "browser-fixture", source_extract_created_at: "2000-01-01T00:00:00Z" },
  assets: [
    { asset_uid: "ui-pdf", role: "pdf", status: "available", filename: "polyimide-sample.pdf", media_type: "application/pdf", size_bytes: 1048576 },
    { asset_uid: "ui-text", role: "full_text", status: "available", filename: "blocks_clean.json", media_type: "application/json", exact_input_verified: true },
    { asset_uid: "ui-si", role: "attachment", status: "unverified", filename: "supplementary-materials.zip", media_type: "application/zip", size_bytes: 2048 }
  ],
  samples: [
    { sample_uid: "ui-sample-1", identity_code: "PI-1", name: "聚酰亚胺样品一", category: "聚酰亚胺", form: "薄膜", characterization: { method: "FTIR" }, evidence: [],
      properties: [{ entry_uid: "ui-property-1", name: "玻璃化转变温度 Tg", value: { single_value: 12.5, min: null, max: null, unit: "°C" }, conditions: { method: "DSC", heating_rate: { value: 10, unit: "°C/min" } }, evidence: [{ quote: "PI-1: Tg 12.5 °C（本地界面样本）", block_id: "23" }] }],
      processes: [{ entry_uid: "ui-process-1", product_name: "PI-1", mass: null, yield_percent: null, conditions: { temperature: { value: 180, unit: "°C" }, atmosphere: "nitrogen" }, post_treatment: "按原始文献记录展示。", evidence: [], materials: [
        { entry_uid: "ui-material-1", name: "Material A", role: "反应物", type: "单体", amounts: [{ kind: "mass", value: { value: 0.25, unit: "g" } }], evidence: [] }
      ] }] },
    { sample_uid: "ui-sample-2", identity_code: null, name: null, category: null, form: null, characterization: null, evidence: [], processes: [],
      properties: [{ entry_uid: "ui-property-2", name: "未注明单位的性质", value: { single_value: 0, unit: null }, conditions: null, evidence: [] }] }
  ]
};

const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
const results = [];
const failures = [];
try {
  for (const [width, height] of [[1440, 900], [1024, 768], [390, 844]]) {
    const context = await browser.newContext({ viewport: { width, height }, reducedMotion: "reduce", serviceWorkers: "block" });
    const calls = [];
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.startsWith("/api/")) {
        calls.push({ path: url.pathname, method: request.method(), body: request.postData() ? request.postDataJSON() : null });
        if (url.pathname.endsWith("/auth/session")) return route.fulfill({ json: { authenticated: true, user: { id: "ui-user", username: "UI Fixture", must_change_password: false }, session_id: "ui-session", csrf_token: "ui-csrf", capabilities: {} } });
        if (url.pathname.endsWith("/knowledge/sources")) return route.fulfill({ json: { sources: [{ data_source: "legacy", enabled: true, ready: true, total: 1 }, { data_source: "extracted_literature", enabled: true, ready: true, total: 1, generation_id: paper.generation_id }] } });
        if (url.pathname.endsWith("/knowledge/search")) {
          const body = request.postDataJSON();
          return route.fulfill({ json: { data_source: "extracted_literature", generation_id: paper.generation_id, search_id: "ui-search", query: body.query, groups: body.groups, terms: ["polyimide"], page: 1, page_size: body.page_size, total: 1, query_time_ms: 8, results: [paper] } });
        }
        if (url.pathname.endsWith("/knowledge/observations")) return route.fulfill({ json: { event: "article.opened", search_id: "ui-search", document_uid: paper.document_uid } });
        if (url.pathname.endsWith("/literature/ui-paper/fulltext")) return route.fulfill({ json: { document_uid: paper.document_uid, snapshot_uid: paper.snapshot_uid, exact_input_verified: true, next_cursor: null, blocks: [
          { block_id: "22", type: "heading", text: "热性能", heading: "热性能", parts: [{ type: "text", text: "热性能" }] },
          { block_id: "23", type: "table", text: "", heading: "热性能", parts: [{ type: "table", rows: [["样品", "Tg (°C)", "测试条件", "备注"], ["PI-1", "12.5", "DSC, 10 °C/min", "仅用于界面布局验证"]] }] }
        ] } });
        if (url.pathname.endsWith("/literature/ui-paper")) return route.fulfill({ json: detail });
        return route.fulfill({ status: 503, json: { detail: "Local browser fixture: service unused" } });
      }
      if (url.origin !== base || !["GET", "HEAD"].includes(request.method())) return route.abort();
      return route.continue();
    });
    try {
      await page.goto(`${base}/knowledge`, { waitUntil: "domcontentloaded" });
      await page.getByRole("combobox", { name: "知识库数据来源" }).selectOption("extracted_literature");
      await page.getByRole("searchbox", { name: "本地知识库检索词" }).fill("polyimide");
      await page.getByLabel("文献起始年份").fill("2020");
      await page.getByLabel("有性质信息").check();
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-form.png`) });
      const form = await page.evaluate(() => {
        const controls = [...document.querySelectorAll(".ks-source-controls select,.ks-source-controls button,.ks-literature-filters input,.ks-local-form input,.ks-local-form select,.ks-local-form button")];
        const rects = controls.map((element) => { const r = element.getBoundingClientRect(); return { x:r.x,y:r.y,right:r.right,bottom:r.bottom,w:r.width,h:r.height }; });
        let overlaps = 0;
        for (let i=0; i<rects.length; i++) for (let j=i+1; j<rects.length; j++) {
          const a=rects[i],b=rects[j]; if (Math.min(a.right,b.right)-Math.max(a.x,b.x)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1) overlaps++;
        }
        return { overlaps, horizontalOverflow: document.documentElement.scrollWidth>innerWidth+1, zeroSizedControls:rects.filter(r=>r.w<1||r.h<1).length };
      });
      assert.equal(form.overlaps, 0, "search controls overlap");
      assert.equal(form.horizontalOverflow, false, "search form overflows viewport");
      assert.equal(form.zeroSizedControls, 0, "search control is unreachable");
      await page.getByRole("button", { name: "运行检索", exact: true }).click();
      if (width < 900) await page.getByRole("button", { name: /^聚酰亚胺样品的热性能/ }).click();
      const drawer = page.getByRole("dialog", { name: "文献详情" });
      await drawer.waitFor();
      await page.getByRole("tab", { name: "已提取信息", exact: true }).click();
      await page.getByLabel("文献样品").selectOption("ui-sample-1");
      await page.getByText("12.5", { exact: true }).waitFor();
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-samples.png`) });
      await page.getByLabel("文献样品").selectOption("ui-sample-2");
      await page.getByText("未命名样品", { exact: true }).waitFor();
      assert.equal(await drawer.getByText("Material A", { exact: true }).count(), 0, "sample data leaked");
      assert.equal(await drawer.getByText("°C", { exact: true }).count(), 0, "missing unit was filled");
      await page.getByRole("tab", { name: "原文与来源", exact: true }).click();
      await page.getByRole("cell", { name: "12.5", exact: true }).waitFor();
      const link = await page.getByRole("link", { name: "打开 PDF", exact: true }).getAttribute("href");
      assert.ok(link.startsWith("/api/v1/knowledge/literature/ui-paper/files/ui-pdf/content?"));
      assert.equal(new URL(link,base).searchParams.get("session_context"), "ui-session");
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-original.png`) });
      // Revisit an already loaded full text through evidence: the controlled tab
      // must mount before the parent attempts to scroll and focus its target.
      await page.getByRole("tab", { name: "已提取信息", exact: true }).click();
      await page.getByLabel("文献样品").selectOption("ui-sample-1");
      await page.getByText("查看原始依据（1）", { exact: true }).click();
      await page.getByRole("button", { name: "查看正文", exact: true }).click();
      const evidenceTarget = drawer.locator(".is-evidence-target");
      await evidenceTarget.waitFor();
      assert.equal(await evidenceTarget.evaluate(element => element === document.activeElement), true, "evidence target did not receive focus");
      const evidence = await evidenceTarget.evaluate(element => {
        const r = element.getBoundingClientRect();
        const body = element.closest(".ks-drawer-body").getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, bodyTop: body.top, bodyBottom: body.bottom };
      });
      assert.ok(evidence.top >= evidence.bodyTop - 1 && evidence.bottom <= evidence.bodyBottom + 1, "evidence target was not scrolled into view");
      assert.equal(calls.filter(call => call.path.endsWith("/literature/ui-paper/fulltext")).length, 1, "evidence navigation reloaded full text");
      const geometry = await drawer.evaluate((element) => {
        const r=element.getBoundingClientRect();
        return { x:r.x,y:r.y,right:r.right,bottom:r.bottom,viewportWidth:innerWidth,viewportHeight:innerHeight,
          horizontalOverflow:document.documentElement.scrollWidth>innerWidth+1,
          drawerOverflow:element.scrollWidth>element.clientWidth+1 };
      });
      assert.equal(geometry.horizontalOverflow,false,"page horizontal overflow");
      assert.equal(geometry.drawerOverflow,false,"drawer horizontal overflow");
      assert.ok(geometry.x>=-1 && geometry.right<=width+1,"drawer outside horizontal viewport");
      await page.keyboard.press("Escape");
      await drawer.waitFor({state:"hidden"});
      await page.getByRole("button",{name:"查看文献详情",exact:true}).click();
      await drawer.waitFor();
      assert.equal(errors.length,0,errors.join("\n"));
      results.push({ viewport:[width,height], form, geometry, evidence, observations:calls.filter(c=>c.path.endsWith("/observations")).map(c=>c.body.source), apiCalls:calls.length });
    } catch(error) {
      failures.push({viewport:[width,height],message:error.message,errors});
      await page.screenshot({path:resolve(output,`failure-${width}.png`)});
    }
    await context.close();
  }
} finally {
  await browser.close();
}
await writeFile(resolve(output,"verification.json"),JSON.stringify({results,failures},null,2));
console.log(JSON.stringify({output,results,failures},null,2));
assert.equal(failures.length,0,"literature browser checks failed");
