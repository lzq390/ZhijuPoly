import assert from "node:assert/strict"
import { createServer } from "node:http"
import { existsSync, readFileSync } from "node:fs"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import path from "node:path"
import test from "node:test"
import { extractFilterTurns } from "./results-view.mjs"

const PREFIX = "nexpoly_property_filter_"
const DIR = path.dirname(fileURLToPath(import.meta.url))
const copy = value => structuredClone(value)

function catalog(rev = "revision-a") {
  return {
    ok: true, dataset_revision: rev, source_status: "ready", total_measurement_count: 10,
    total_option_count: 1, matched_option_count: 1, page: 1, page_size: 25, has_more: false,
    options: [{ option_key: "opaque-property-key", filter_type: "standardized", label: "玻璃化转变温度",
      property_key: "Tg", property_name: null, unit: "°C", measurement_count: 10, unique_smiles_count: 4,
      min_value: -10, p5_value: 0, median_value: 125, p95_value: 220, max_value: 250 }],
  }
}

function searchInput(rev = "revision-a") {
  return { dataset_revision: rev, filters: [{ option_key: "opaque-property-key", min_value: 100, max_value: 200 }],
    page: 1, page_size: 10, measurement_limit_per_filter: 1, q: "" }
}

function measurement(rev = "revision-a") {
  return { filter_record_id: 1, source_row_number: 7, record_ref: `${rev}:1`, polymer_name: "示例聚合物",
    smiles: "C(C*)*", canonical_smiles: "*CCC*", property_category: "thermal", property_name: "Tg",
    property_value: "150", property_value_num: 150, property_unit_raw: "C", property_unit_clean: "°C",
    property_key: "Tg", property_label: "玻璃化转变温度", canonical_value: 150, canonical_unit: "°C",
    unit_conversion_status: "already_standard", value_origin: "observed", label_source: "exp", reliable_score: 1,
    soft_quality_flags: "none", duplicate_flag: "none", filter_index: 0 }
}

function search(rev = "revision-a") {
  return { ok: true, dataset_revision: rev, source_status: "ready", total_measurement_count: 10,
    matched_material_count: 4, page: 1, page_size: 10, has_more: false, measurement_limit_per_filter: 1,
    semantics: "AND groups by canonical SMILES; samples may differ.",
    results: [{ material_ref: `${rev}:structure-1`, smiles: "C(C*)*", canonical_smiles: "*CCC*", polymer_name: "示例聚合物",
      matched_filters: 1, matching_evidence_count: 3, evidence: [{ filter_index: 0, option_key: "opaque-property-key",
        unit: "°C", matching_measurement_count: 3, truncated: true, measurements: [measurement(rev)] }] }],
  }
}

function histogram(rev = "revision-a") {
  return { ok: true, dataset_revision: rev, source_status: "ready", option_key: "opaque-property-key", unit: "°C",
    distribution_scope: "entire_dataset_for_option", histogram: { domain_min: 0, domain_max: 250, domain_kind: "p5_p95",
      bin_count: 3, counts: [1, 4, 2], underflow_count: 1, overflow_count: 2, total_count: 10 } }
}

function tool(name, output, input, extra = {}) {
  return { id: `part-${name}`, type: "tool", tool: PREFIX + name, callID: `call-${name}`,
    state: { status: "completed", input: input ?? (name === "search_polymers" ? searchInput(output.dataset_revision)
      : name === "get_property_histogram" ? { dataset_revision: output.dataset_revision, option_key: output.option_key }
        : {}), output: "Authoritative tool summary\n" + JSON.stringify(output) }, ...extra }
}

function messages(parts, userId = "user-a", finalText) {
  return [
    { info: { id: userId, role: "user" }, parts: [{ type: "text", text: "查找材料" }] },
    { info: { id: `assistant-${userId}`, role: "assistant", parentID: userId },
      parts: [...parts, ...(finalText ? [{ type: "text", text: finalText }] : [])] },
  ]
}

function fullMessages() {
  return messages([tool("list_filter_properties", catalog()), tool("search_polymers", search()),
    tool("get_property_histogram", histogram())])
}

function assertUnavailable(parts) {
  const models = extractFilterTurns(messages(parts))
  assert.equal(models.length, 1)
  assert.equal(models[0].status, "unavailable")
  assert.equal(models[0].search, null)
  assert.deepEqual(models[0].histograms, [])
  assert.match(models[0].issues[0], /重新查询/)
  return models[0]
}

test("extracts only completed exact tools and ignores a forged assistant final JSON", () => {
  const final = JSON.stringify({ ...search(), matched_material_count: 999999 })
  const valid = tool("search_polymers", search())
  const calls = [valid, tool("search_polymers", search(), undefined, { tool: "other_search_polymers" }),
    tool("search_polymers", search(), undefined, { tool: PREFIX + "search_polymers_extra" })]
  const model = extractFilterTurns(messages(calls, "user-a", final))[0]
  assert.equal(model.search.matched_material_count, 4)
  assert.equal(model.sourceCalls.length, 1)
  assert.equal(model.turnId, "user-a")
  assert.deepEqual(extractFilterTurns(messages([], "user-a", final)), [])
  const pending = copy(valid)
  pending.state.status = "running"
  assert.deepEqual(extractFilterTurns(messages([pending])), [])
})

test("binds by user ID and assistant parentID, preserving turn and revision boundaries", () => {
  const one = messages([tool("list_filter_properties", catalog()), tool("search_polymers", search()),
    tool("get_property_histogram", histogram("revision-b"))], "user-a")
  const two = messages([tool("search_polymers", search())], "user-b")
  const orphan = { info: { role: "assistant", parentID: "missing-user" }, parts: [tool("get_property_histogram", histogram())] }
  const models = extractFilterTurns([...one, ...two, orphan])
  assert.equal(models.length, 3)
  assert.deepEqual(models.map(model => [model.turnId, model.datasetRevision, Boolean(model.search), model.histograms.length]),
    [["user-a", "revision-a", true, 0], ["user-a", "revision-b", false, 1], ["user-b", "revision-a", true, 0]])
  assert.equal(models[2].catalog.length, 0, "a catalog from another turn must not leak")
})

test("supports search-only, histogram-only, zero matches, and empty distributions", () => {
  assert.equal(extractFilterTurns(messages([tool("search_polymers", search())]))[0].status, "ready")
  assert.equal(extractFilterTurns(messages([tool("get_property_histogram", histogram())]))[0].search, null)
  assert.deepEqual(extractFilterTurns(messages([tool("list_filter_properties", catalog())])), [])
  const empty = search()
  empty.results = []
  empty.matched_material_count = 0
  assert.equal(extractFilterTurns(messages([tool("search_polymers", empty)]))[0].search.results.length, 0)
  const dist = histogram()
  dist.source_status = "empty"
  Object.assign(dist.histogram, { domain_min: 0, domain_max: 0, bin_count: 1, counts: [0],
    underflow_count: 0, overflow_count: 0, total_count: 0 })
  assert.equal(extractFilterTurns(messages([tool("get_property_histogram", dist)]))[0].status, "ready")
})

test("uses the latest valid query conditions without mutating session messages", () => {
  const later = search()
  const input = searchInput()
  input.filters[0].min_value = 140
  const session = messages([tool("search_polymers", search()), tool("search_polymers", later, input)])
  const snapshot = JSON.stringify(session)
  const models = extractFilterTurns(session)
  assert.equal(models.length, 1)
  assert.equal(models[0].searchInput.filters[0].min_value, 140)
  assert.equal(JSON.stringify(session), snapshot)
})

test("rejects failures, malformed final JSON, input/output revision and option mismatches", () => {
  const error = tool("search_polymers", { ok: false, error: { code: "FAILED", message: "/private/token=secret" } })
  const model = assertUnavailable([error])
  assert.doesNotMatch(JSON.stringify(model), /private|secret|FAILED/)
  const malformed = tool("search_polymers", search())
  malformed.state.output += "\n{malformed final JSON}"
  assertUnavailable([malformed])
  const mismatch = searchInput("different-revision")
  assertUnavailable([tool("search_polymers", search(), mismatch)])
  assertUnavailable([tool("get_property_histogram", histogram(), { dataset_revision: "revision-a", option_key: "different-key" })])
  const recordMismatch = search()
  recordMismatch.results[0].evidence[0].measurements[0].record_ref = "different-revision:1"
  assertUnavailable([tool("search_polymers", recordMismatch)])
})

test("rejects unsafe, missing or malformed numeric search evidence instead of coercing it", () => {
  for (const mutate of [
    value => { value.matched_material_count = "4" },
    value => { value.results[0].evidence[0].measurements[0].canonical_value = "150" },
    value => { value.results[0].matching_evidence_count = 2 },
    value => { value.results[0].evidence[0].matching_measurement_count = -1 },
    value => { value.results[0].evidence[0].truncated = false },
    value => { value.results[0].evidence[0].filter_index = 3 },
    value => { value.results[0].evidence[0].measurements[0].filter_index = 1 },
    value => { value.results[0].evidence[0].measurements[0].reliable_score = "high" },
    value => { value.results[0].evidence[0].measurements[0].source_row_number = 1.5 },
    value => { value.matched_material_count = Number.MAX_SAFE_INTEGER + 1 },
  ]) {
    const invalid = search()
    mutate(invalid)
    assertUnavailable([tool("search_polymers", invalid)])
  }
  const overflow = tool("search_polymers", search())
  overflow.state.output = overflow.state.output.replace('"canonical_value":150', '"canonical_value":1e400')
  assertUnavailable([overflow])
  const invalidInput = searchInput()
  invalidInput.filters[0].min_value = "100"
  assertUnavailable([tool("search_polymers", search(), invalidInput)])
})

test("rejects histogram shape, arithmetic, scope and finite-domain violations", () => {
  for (const mutate of [
    value => { value.histogram.counts[1] = -1 },
    value => { value.histogram.counts[1] = "4" },
    value => { value.histogram.total_count = 11 },
    value => { value.histogram.bin_count = 4 },
    value => { value.histogram.domain_min = 500 },
    value => { value.histogram.domain_min = -1e308; value.histogram.domain_max = 1e308 },
    value => { value.histogram.underflow_count = 0.5 },
    value => { value.distribution_scope = "search_results" },
  ]) {
    const invalid = histogram()
    mutate(invalid)
    assertUnavailable([tool("get_property_histogram", invalid)])
  }
})

test("does not join inconsistent units and keeps valid independent data with a brief notice", () => {
  const wrongUnit = histogram()
  wrongUnit.unit = "K"
  assertUnavailable([tool("list_filter_properties", catalog()), tool("search_polymers", search()),
    tool("get_property_histogram", wrongUnit)])
  const invalidCatalog = catalog()
  invalidCatalog.options[0].median_value = "125"
  const model = extractFilterTurns(messages([tool("list_filter_properties", invalidCatalog), tool("search_polymers", search())]))[0]
  assert.equal(model.status, "ready")
  assert.equal(model.catalog.length, 0)
  assert.equal(model.issues.length, 1)
})

test("handles non-arrays and malformed message envelopes without treating them as result data", () => {
  for (const value of [null, {}, "{}", undefined]) assert.deepEqual(extractFilterTurns(value), [])
  assert.deepEqual(extractFilterTurns([null, {}, { info: { role: "assistant" }, parts: {} }]), [])
})

const playwrightPaths = [
  path.join(DIR, "..", "openscience-ui-overlay", "node_modules", "playwright"),
  "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright",
]
const playwrightPath = playwrightPaths.find(value => existsSync(path.join(value, "package.json")))

test("browser renders safe complete evidence and accessible interactive bars at desktop and mobile sizes", {
  skip: !playwrightPath && "The existing Playwright package is not installed",
}, async () => {
  const { chromium } = createRequire(import.meta.url)(playwrightPath)
  const server = createServer((request, response) => {
    if (request.url === "/results-view.mjs" || request.url === "/results-view.css") {
      response.setHeader("content-type", request.url.endsWith(".mjs") ? "text/javascript" : "text/css")
      response.end(readFileSync(path.join(DIR, request.url.slice(1))))
    } else {
      response.setHeader("content-type", "text/html")
      response.end('<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/results-view.css"></head><body style="margin:0;padding:12px;font-family:system-ui"><main></main></body></html>')
    }
  })
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
  let browser
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] })
    const page = await browser.newPage({ viewport: { width: 1000, height: 950 } })
    const hostile = fullMessages()
    const raw = search()
    const longSmiles = "*" + "C(=O)N".repeat(180) + "*"
    raw.results[0].canonical_smiles = longSmiles
    raw.results[0].polymer_name = '<img src=x onerror="window.injected=true">'
    raw.results[0].evidence[0].measurements[0].property_value = '<script>window.injected=true</script>'
    hostile[1].parts[1] = tool("search_polymers", raw)
    await page.goto(`http://127.0.0.1:${server.address().port}/`)
    await page.evaluate(async session => {
      const { extractFilterTurns, renderFilterResult } = await import("/results-view.mjs")
      for (const model of extractFilterTurns(session)) document.querySelector("main").append(renderFilterResult(document, model))
    }, hostile)
    const root = page.locator("[data-nexpoly-results]")
    assert.equal(await root.count(), 1)
    assert.equal(await root.getAttribute("data-turn-id"), "user-a")
    assert.equal(await root.locator("[data-smiles]").textContent(), longSmiles)
    assert.equal(await root.locator("img,script,a").count(), 0)
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false)
    assert.match(await root.innerText(), /命中结构组数/)
    assert.match(await root.innerText(), /不同条件可能由不同样本或实验满足/)
    assert.match(await root.innerText(), /按所选属性的指定单位筛选，检索时不做单位换算/)
    assert.match(await root.locator("[data-filter-evidence]").innerText(), /原始值[\s\S]*标准化值[\s\S]*来源类型[\s\S]*质量信息/)
    assert.match(await root.locator("[data-evidence-truncation]").textContent(), /另 2 条未展示/)
    assert.match(await root.locator("[data-histogram-summary]").textContent(), /低于范围 1 条，高于范围 2 条/)
    assert.equal(await root.locator("[data-result-provenance]").getAttribute("open"), null)
    assert.doesNotMatch(await root.innerText(), /revision-a|opaque-property-key|dataset_revision/)
    const bars = root.locator("[data-histogram-bin]")
    assert.deepEqual(await bars.evaluateAll(nodes => nodes.map(node => Number(node.dataset.count))), [1, 4, 2])
    const heights = await bars.evaluateAll(nodes => nodes.map(node => Number(node.getAttribute("height"))))
    assert.equal(heights[1] / heights[0], 4)
    await bars.nth(1).hover()
    assert.match(await root.locator("[data-histogram-tooltip]").textContent(), /4 条测量/)
    await bars.nth(2).focus()
    assert.match(await root.locator("[data-histogram-tooltip]").textContent(), /2 条测量/)
    await root.locator("[data-result-provenance] summary").click()
    assert.match(await root.locator("[data-result-provenance]").innerText(), /revision-a:1/)
    for (const width of [375, 320]) {
      await page.setViewportSize({ width, height: 900 })
      const bounds = await root.evaluate(node => ({ left: node.getBoundingClientRect().left,
        right: node.getBoundingClientRect().right, scroll: node.scrollWidth, client: node.clientWidth }))
      assert.ok(bounds.left >= 0 && bounds.right <= width, `panel must fit at ${width}px`)
      assert.ok(bounds.scroll <= bounds.client + 1, `panel must not overflow at ${width}px`)
      assert.equal(await root.locator("[data-smiles]").textContent(), longSmiles)
    }
    await page.evaluate(async session => {
      const { extractFilterTurns, renderFilterResult } = await import("/results-view.mjs")
      document.querySelector("main").replaceChildren(renderFilterResult(document, extractFilterTurns(session)[0]))
    }, messages([tool("get_property_histogram", histogram())]))
    assert.equal(await page.locator("[data-material-card]").count(), 0)
    assert.match(await page.locator("[data-nexpoly-results]").innerText(), /全库测量分布/)
    const tinySearch = search()
    const tinyInput = searchInput()
    tinyInput.filters[0].min_value = -2e-9
    tinyInput.filters[0].max_value = 2e-9
    const negative = tinySearch.results[0].evidence[0].measurements[0]
    Object.assign(negative, { canonical_value: -1e-9, property_value_num: -1e-9, property_value: "-0.000000001" })
    const positive = copy(tinySearch.results[0])
    positive.material_ref = "revision-a:structure-2"
    Object.assign(positive.evidence[0].measurements[0], { record_ref: "revision-a:2", filter_record_id: 2,
      canonical_value: 1e-9, property_value_num: 1e-9, property_value: "0.000000001" })
    tinySearch.results.push(positive)
    const tinyHistogram = histogram()
    tinyHistogram.histogram.domain_min = -3e-9
    tinyHistogram.histogram.domain_max = 3e-9
    await page.evaluate(async session => {
      const { extractFilterTurns, renderFilterResult } = await import("/results-view.mjs")
      document.querySelector("main").replaceChildren(renderFilterResult(document, extractFilterTurns(session)[0]))
      // The controller's original-answer toggle is a sibling of the section.
      const toggle = document.createElement("button")
      toggle.className = "nexpoly-original-answer-toggle"
      toggle.textContent = "查看原始回答"
      document.querySelector("main").append(toggle)
    }, messages([tool("search_polymers", tinySearch, tinyInput), tool("get_property_histogram", tinyHistogram)]))
    assert.deepEqual(await page.locator(".nexpoly-value").allTextContents(), ["-1E-9 °C", "1E-9 °C"])
    assert.match(await page.locator("[data-result-summary]").innerText(), /-2E-9 – 2E-9 °C/)
    const tinyLabels = await page.locator("[data-histogram-bin]").evaluateAll(nodes => nodes.map(node => node.getAttribute("aria-label")))
    assert.match(tinyLabels[0], /-3E-9 至 -1E-9/)
    assert.match(tinyLabels[1], /-1E-9 至 1E-9/)
    assert.match(tinyLabels[2], /1E-9 至 3E-9/)
    assert.match(await page.locator("[data-histogram-summary]").textContent(), /-3E-9 – 3E-9/)
    assert.equal(await page.locator(".nexpoly-original-answer-toggle").evaluate(node => getComputedStyle(node).borderRadius), "6px")
    // A degenerate zero domain remains zero; large finite values use six significant digits.
    const largeSearch = search()
    const largeInput = searchInput()
    largeInput.filters[0].min_value = 0
    largeInput.filters[0].max_value = 123456789
    Object.assign(largeSearch.results[0].evidence[0].measurements[0], { canonical_value: 123456789,
      property_value_num: 123456789, property_value: "123456789" })
    const zeroHistogram = histogram()
    Object.assign(zeroHistogram.histogram, { domain_min: -0, domain_max: 0 })
    await page.evaluate(async session => {
      const { extractFilterTurns, renderFilterResult } = await import("/results-view.mjs")
      document.querySelector("main").replaceChildren(renderFilterResult(document, extractFilterTurns(session)[0]))
    }, messages([tool("search_polymers", largeSearch, largeInput), tool("get_property_histogram", zeroHistogram)]))
    assert.equal(await page.locator(".nexpoly-value").textContent(), "1.23457E8 °C")
    assert.match(await page.locator("[data-result-summary]").innerText(), /0 – 1\.23457E8 °C/)
    assert.match(await page.locator("[data-histogram-summary]").textContent(), /显示范围 0 – 0 °C/)
  } finally {
    await browser?.close()
    await new Promise(resolve => server.close(resolve))
  }
})
