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
const patent = {
  data_source: "legacy", knowledge_id: 900001, source_file: "synthetic-patents.jsonl", source_row_number: 1, source_sequence: null,
  title_zh: "聚酰亚胺合成专利（界面验证样本）", title_en: null, abstract: "polyimide 专利界面验证样本。", abstract_snippet: "polyimide 专利界面验证样本。",
  claim: null, analysis: null, is_polymer_synthesis: null, judgement_reason: null, polymer_iupac: null, formulation: null,
  catalyst: null, temperature: null, reaction_time: null, solvent: null, matched_terms: ["polyimide"], matched_fields: ["abstract"]
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

async function measureControls(scope) {
  return scope.evaluate((element) => {
    const controls = [...element.querySelectorAll("input,select,button")].filter((control) => !control.closest('[inert], [hidden], [aria-hidden="true"]'));
    const rects = controls.map((control) => { const r = control.getBoundingClientRect(); return { x:r.x,y:r.y,right:r.right,bottom:r.bottom,w:r.width,h:r.height }; });
    let overlaps = 0;
    for (let i=0; i<rects.length; i++) for (let j=i+1; j<rects.length; j++) {
      const a=rects[i],b=rects[j]; if (Math.min(a.right,b.right)-Math.max(a.x,b.x)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1) overlaps++;
    }
    return { overlaps, horizontalOverflow: document.documentElement.scrollWidth>innerWidth+1, scopeOverflow: element.scrollWidth>element.clientWidth+1, zeroSizedControls:rects.filter(r=>r.w<1||r.h<1).length };
  });
}

function isSearchResponse(response, source) {
  return new URL(response.url()).pathname.endsWith("/knowledge/search")
    && (response.request().postDataJSON()?.data_source ?? "legacy") === source;
}

const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
const results = [];
const failures = [];
try {
  for (const [width, height] of [[1440, 900], [1024, 768], [390, 844]]) {
    const context = await browser.newContext({ viewport: { width, height }, reducedMotion: "reduce", serviceWorkers: "block" });
    const calls = [];
    const searchCalls = () => calls.filter((call) => call.path.endsWith("/knowledge/search"));
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
          const literature = body.data_source === "extracted_literature";
          return route.fulfill({ json: { data_source: literature ? "extracted_literature" : "legacy", ...(literature ? { generation_id: paper.generation_id } : {}), search_id: literature ? "ui-search" : "ui-patent-search", query: body.query, groups: body.groups, terms: ["polyimide"], page: body.page, page_size: body.page_size, total: 1, query_time_ms: 8, results: [literature ? paper : patent] } });
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
      await page.getByRole("searchbox", { name: "本地知识库检索词" }).fill("polyimide");
      const initialForm = await measureControls(page.locator(".ks-local-form"));
      assert.equal(initialForm.overlaps, 0, "initial search controls overlap");
      assert.equal(initialForm.horizontalOverflow, false, "initial search form overflows viewport");
      assert.equal(initialForm.scopeOverflow, false, "initial search form overflows horizontally");
      assert.equal(initialForm.zeroSizedControls, 0, "initial search control is unreachable");
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-initial-form.png`) });
      await page.getByRole("button", { name: "条件设置", exact: true }).click();
      const conditionsDialog = page.getByRole("dialog", { name: "条件设置", exact: true });
      await conditionsDialog.waitFor();
      await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "关闭条件设置");
      await page.keyboard.press("Shift+Tab");
      const reverseWrap = await conditionsDialog.getByRole("button", { name: "应用条件", exact: true }).evaluate((element) => element === document.activeElement);
      assert.equal(reverseWrap, true, "condition dialog did not wrap reverse keyboard focus");
      await page.keyboard.press("Tab");
      const forwardWrap = await conditionsDialog.getByRole("button", { name: "关闭条件设置", exact: true }).evaluate((element) => element === document.activeElement);
      assert.equal(forwardWrap, true, "condition dialog did not wrap forward keyboard focus");
      await page.keyboard.press("Escape");
      await conditionsDialog.waitFor({ state: "hidden" });
      await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "条件设置");
      const keyboard = { reverseWrap, forwardWrap, escapeFocusRestored: true };
      await page.getByRole("button", { name: "条件设置", exact: true }).click();
      await conditionsDialog.waitFor();
      assert.equal(await conditionsDialog.getByRole("checkbox", { name: "现有专利知识库", exact: true }).isChecked(), true);
      await conditionsDialog.getByRole("checkbox", { name: "提取文献库", exact: true }).check();
      await conditionsDialog.getByRole("checkbox", { name: "现有专利知识库", exact: true }).uncheck();
      const yearFrom = conditionsDialog.getByRole("combobox", { name: "文献起始年份", exact: true });
      const yearTo = conditionsDialog.getByRole("combobox", { name: "文献结束年份", exact: true });
      const currentYear = new Date().getUTCFullYear();
      const menu = (label) => conditionsDialog.getByRole("listbox", { name: label, exact: true });
      const choose = async (label, optionLabel) => {
        await conditionsDialog.getByRole("combobox", { name: label, exact: true }).click();
        const list = menu(label);
        await list.getByRole("option", { name: optionLabel, exact: true }).click();
        await list.waitFor({ state: "hidden" });
      };
      assert.equal(await conditionsDialog.locator("xpath=..").evaluate((element) => element.classList.contains("np-workbench-theme")), true, "portal lost the platform theme");
      await yearFrom.click();
      const yearList = menu("文献起始年份");
      await yearList.waitFor();
      await yearList.getByRole("option", { name:"不限", exact:true }).waitFor();
      const yearOptions = (await yearList.locator('[role="option"]').allTextContents()).map((label) => label.trim());
      assert.equal(yearOptions[0], "不限", "year dropdown lost its unlimited option");
      assert.equal(yearOptions[1], `${currentYear}年`, "year dropdown does not start at the current year");
      assert.equal(yearOptions.at(-1), "1900年", "year dropdown has an incorrect historical lower bound");
      assert.equal(yearOptions.length, currentYear - 1900 + 2);
      const yearMenuGeometry = await yearList.evaluate((element) => {
        const r = element.getBoundingClientRect(), body = element.closest(".ks-conditions-body").getBoundingClientRect();
        return { height:r.height, top:r.top, bottom:r.bottom, bodyTop:body.top, bodyBottom:body.bottom, scrollable:element.scrollHeight>element.clientHeight, themeBlue:getComputedStyle(element).getPropertyValue("--np-theme-blue").trim() };
      });
      assert.ok(yearMenuGeometry.height > 50 && yearMenuGeometry.height <= 210, "year menu is too tall or unusably short");
      assert.equal(yearMenuGeometry.scrollable, true, "full year range does not scroll");
      assert.equal(yearMenuGeometry.themeBlue, "#2563eb", "dropdown lost shared theme tokens");
      assert.ok(yearMenuGeometry.top >= yearMenuGeometry.bodyTop - 1 && yearMenuGeometry.bottom <= yearMenuGeometry.bodyBottom + 1, "year menu is clipped by the modal body");
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-year-menu.png`) });
      await page.keyboard.press("Escape");
      await yearList.waitFor({ state: "hidden" });
      assert.equal(await conditionsDialog.isVisible(), true, "first Escape closed the parent modal");
      await choose("文献起始年份", "2025年");
      await yearTo.click();
      assert.equal(await menu("文献结束年份").getByRole("option", { name:"2024年", exact:true }).isDisabled(), true, "end dropdown allows a year before its start");
      await menu("文献结束年份").getByRole("option", { name:"2025年", exact:true }).click();
      await menu("文献结束年份").waitFor({ state:"hidden" });
      await yearFrom.click();
      assert.equal(await yearList.getByRole("option", { name:"2026年", exact:true }).isDisabled(), true, "start dropdown allows a year after its end");
      await page.keyboard.press("Escape");
      await yearList.waitFor({ state:"hidden" });
      assert.equal(await conditionsDialog.getByRole("button", { name: "应用条件", exact: true }).isEnabled(), true, "equal start/end years are rejected");
      await choose("文献起始年份", "2020年");
      await choose("文献结束年份", "2024年");
      await yearFrom.focus();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await yearList.waitFor({ state:"hidden" });
      assert.equal((await yearFrom.innerText()).trim(), "2019年", "year dropdown keyboard navigation failed");
      await choose("文献起始年份", "2020年");
      assert.equal(await conditionsDialog.getByRole("button", { name: /重新检查来源|刷新知识库状态/ }).count(), 0, "removed source refresh action is still visible");
      await conditionsDialog.getByRole("checkbox", { name: "有性质信息", exact: true }).check();
      const pageSize = conditionsDialog.getByRole("combobox", { name: "每库每页数量", exact: true });
      await pageSize.click();
      await menu("每库每页数量").getByRole("option", { name:"20 条", exact:true }).waitFor();
      assert.deepEqual((await menu("每库每页数量").getByRole("option").allTextContents()).map((label) => label.trim()), ["20 条", "50 条", "100 条"]);
      await menu("每库每页数量").getByRole("option", { name:"50 条", exact:true }).click();
      await menu("每库每页数量").waitFor({ state:"hidden" });
      const conditions = await measureControls(conditionsDialog);
      assert.equal(conditions.overlaps, 0, "condition controls overlap");
      assert.equal(conditions.horizontalOverflow, false, "conditions overflow viewport");
      assert.equal(conditions.scopeOverflow, false, "conditions dialog overflows horizontally");
      assert.equal(conditions.zeroSizedControls, 0, "condition control is unreachable");
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-conditions.png`) });
      await conditionsDialog.getByRole("button", { name: "应用条件", exact: true }).click();
      await conditionsDialog.waitFor({ state: "hidden" });
      await page.getByRole("heading", { name: "准备检索已提取文献", exact: true }).waitFor();
      assert.equal(searchCalls().length, 0, "applying conditions automatically searched");
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-form.png`) });
      const form = await measureControls(page.locator(".ks-local-form"));
      assert.equal(form.overlaps, 0, "search controls overlap");
      assert.equal(form.horizontalOverflow, false, "search form overflows viewport");
      assert.equal(form.scopeOverflow, false, "search form overflows horizontally");
      assert.equal(form.zeroSizedControls, 0, "search control is unreachable");
      await Promise.all([
        page.waitForResponse((response) => isSearchResponse(response, "extracted_literature")),
        page.getByRole("button", { name: "运行检索", exact: true }).click()
      ]);
      assert.equal(searchCalls().length, 1, "single-source search issued extra requests");
      assert.equal(searchCalls()[0].body.data_source, "extracted_literature");
      assert.equal(searchCalls()[0].body.page_size, 50);
      assert.deepEqual(searchCalls()[0].body.filters, { year_from: 2020, year_to: 2024, has_properties: true });
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
      const detailCallsBeforeReopen = calls.filter((call) => call.path.endsWith("/literature/ui-paper")).length;
      assert.ok(detailCallsBeforeReopen > 0, "detail endpoint was not queried");
      await page.keyboard.press("Escape");
      await drawer.waitFor({state:"hidden"});
      await page.getByRole("button",{name:"查看文献详情",exact:true}).click();
      await drawer.waitFor();
      assert.equal(calls.filter((call) => call.path.endsWith("/literature/ui-paper")).length, detailCallsBeforeReopen, "reopening detail reloaded the document");
      await page.keyboard.press("Escape");
      await drawer.waitFor({ state: "hidden" });

      // Cancelling edits must retain both the current result and the applied
      // settings, including nested literature filters.
      const beforeConditionChanges = searchCalls().length;
      const literatureCard = page.locator(".ks-result-list").getByRole("button", { name: /^聚酰亚胺样品的热性能/ });
      await page.getByRole("button", { name: "条件设置", exact: true }).click();
      await conditionsDialog.waitFor();
      await conditionsDialog.getByRole("checkbox", { name: "现有专利知识库", exact: true }).check();
      await choose("文献起始年份", "2022年");
      await conditionsDialog.getByRole("checkbox", { name: "有工艺信息", exact: true }).check();
      await choose("每库每页数量", "100 条");
      await conditionsDialog.getByRole("button", { name: "取消", exact: true }).click();
      await conditionsDialog.waitFor({ state: "hidden" });
      await literatureCard.waitFor();
      assert.equal(searchCalls().length, beforeConditionChanges, "cancelling conditions issued a search");
      await page.getByRole("heading", { name: "文献检索结果", exact: true }).waitFor();
      await page.getByRole("button", { name: "条件设置", exact: true }).click();
      await conditionsDialog.waitFor();
      assert.equal(await conditionsDialog.getByRole("checkbox", { name: "现有专利知识库", exact: true }).isChecked(), false, "cancelled source selection was applied");
      assert.equal(await conditionsDialog.getByRole("checkbox", { name: "提取文献库", exact: true }).isChecked(), true);
      assert.equal((await yearFrom.innerText()).trim(), "2020年", "cancelled year filter was applied");
      assert.equal((await yearTo.innerText()).trim(), "2024年");
      assert.equal(await conditionsDialog.getByRole("checkbox", { name: "有工艺信息", exact: true }).isChecked(), false, "cancelled capability filter was applied");
      assert.equal((await pageSize.innerText()).trim(), "50 条", "cancelled page size was applied");

      // Applying two sources clears old results, then one explicit submission
      // queries each source. Switching their result tabs reuses those results.
      await conditionsDialog.getByRole("checkbox", { name: "现有专利知识库", exact: true }).check();
      await choose("每库每页数量", "100 条");
      await conditionsDialog.getByRole("button", { name: "应用条件", exact: true }).click();
      await conditionsDialog.waitFor({ state: "hidden" });
      await page.getByRole("heading", { name: "准备检索已提取文献", exact: true }).waitFor();
      assert.equal(await literatureCard.count(), 0, "applying conditions retained stale results");
      assert.equal(searchCalls().length, beforeConditionChanges, "applying two sources automatically searched");
      assert.equal(await page.getByRole("searchbox", { name: "本地知识库检索词" }).inputValue(), "polyimide", "applying conditions cleared the query");
      await Promise.all([
        page.waitForResponse((response) => isSearchResponse(response, "legacy")),
        page.waitForResponse((response) => isSearchResponse(response, "extracted_literature")),
        page.getByRole("button", { name: "运行检索", exact: true }).click()
      ]);
      if (width >= 900) {
        await drawer.waitFor();
        await page.keyboard.press("Escape");
        await drawer.waitFor({ state: "hidden" });
      }
      const dualSearches = searchCalls().slice(beforeConditionChanges);
      assert.equal(dualSearches.length, 2, "two-source submission did not query each source exactly once");
      assert.deepEqual(dualSearches.map((call) => call.body.data_source ?? "legacy").sort(), ["extracted_literature", "legacy"]);
      assert.ok(dualSearches.every((call) => call.body.page_size === 100), "per-source page size was not applied");
      assert.equal("filters" in dualSearches.find((call) => !call.body.data_source || call.body.data_source === "legacy").body, false, "literature filters leaked into the patent request");
      assert.deepEqual(dualSearches.find((call) => call.body.data_source === "extracted_literature").body.filters, { year_from: 2020, year_to: 2024, has_properties: true });
      const sourceTabs = page.getByRole("group", { name: "检索结果知识库", exact: true });
      await sourceTabs.waitFor();
      assert.equal(await sourceTabs.getByRole("button").count(), 2);
      const patentTab = sourceTabs.getByRole("button", { name: /^现有专利知识库/ });
      const literatureTab = sourceTabs.getByRole("button", { name: /^提取文献库/ });
      assert.equal(await literatureTab.getAttribute("aria-pressed"), "true");
      await patentTab.click();
      await page.locator(".ks-result-list").getByRole("button", { name: /^聚酰亚胺合成专利/ }).waitFor();
      assert.equal(await patentTab.getAttribute("aria-pressed"), "true");
      assert.equal(await literatureCard.count(), 0, "literature result appeared in the patent tab");
      await literatureTab.click();
      await literatureCard.waitFor();
      assert.equal(await literatureTab.getAttribute("aria-pressed"), "true");
      assert.equal(searchCalls().length, beforeConditionChanges + 2, "switching result sources issued another search");
      await page.screenshot({ path: resolve(output, `literature-${width}x${height}-dual-source.png`) });
      assert.equal(errors.length,0,errors.join("\n"));
      results.push({ viewport:[width,height], initialForm, conditions, keyboard, years:{ min:1900, max:currentYear, sharedThemeDropdown:true, menu:yearMenuGeometry, invertedOptionsDisabled:true, equalYearsAllowed:true, keyboardScrolling:true }, sourceRefreshActionRemoved:true, form, geometry, evidence, searchRequests:searchCalls().map((call) => ({ source:call.body.data_source ?? "legacy", pageSize:call.body.page_size, filters:call.body.filters })), observations:calls.filter(c=>c.path.endsWith("/observations")).map(c=>c.body.source), apiCalls:calls.length });
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
