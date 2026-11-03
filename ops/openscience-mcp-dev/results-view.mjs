// Development-only presentation of authoritative property-filter tool outputs.
// This module is deliberately independent of the model's prose and of the host UI.
const PREFIX = "nexpoly_property_filter_"
const TOOLS = new Set(["list_filter_properties", "search_polymers", "get_property_histogram"])
const NS = "http://www.w3.org/2000/svg"
const object = value => value !== null && typeof value === "object" && !Array.isArray(value)
const string = value => typeof value === "string"
const nonempty = value => string(value) && value.trim().length > 0
const finite = value => typeof value === "number" && Number.isFinite(value)
const count = value => Number.isSafeInteger(value) && value >= 0
const nullableString = value => value == null || string(value)
const nullableNumber = value => value == null || finite(value)
const positive = value => count(value) && value > 0
const revision = value => nonempty(value) && value.length <= 80
const numbers = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 })
const scientificNumbers = new Intl.NumberFormat("zh-CN", { notation: "scientific", maximumSignificantDigits: 6 })
const number = value => value === 0 ? "0"
  : (Math.abs(value) < 1e-4 || Math.abs(value) >= 1e7 ? scientificNumbers : numbers).format(value)

function structuredOutput(part) {
  if (!string(part.state?.output)) return null
  const output = part.state.output.trim()
  // The adapter appends structuredContent on the final line after its summary.
  // Never search prose or the assistant's final answer for a convenient object.
  const candidates = [output, output.slice(output.lastIndexOf("\n") + 1).trim()]
  for (const candidate of candidates) {
    if (!candidate.startsWith("{")) continue
    try {
      const result = JSON.parse(candidate)
      if (object(result) && result.ok === true && !Object.hasOwn(result, "error") && revision(result.dataset_revision)
          && ["ready", "empty"].includes(result.source_status)) return result
    } catch { /* An incomplete or unsuccessful tool output is not display data. */ }
  }
  return null
}

function validatePage(value, maxPageSize) {
  return positive(value.page) && positive(value.page_size) && value.page_size <= maxPageSize
    && typeof value.has_more === "boolean" && count(value.total_measurement_count)
}

function validateCatalog(value, input) {
  if (!validatePage(value, 50) || !count(value.total_option_count) || !count(value.matched_option_count)
      || value.matched_option_count > value.total_option_count || !Array.isArray(value.options)
      || value.options.length > value.page_size || value.options.length > value.matched_option_count) return false
  if (input.dataset_revision != null && input.dataset_revision !== value.dataset_revision) return false
  if ((input.page ?? 1) !== value.page || (input.page_size ?? 25) !== value.page_size) return false
  const seen = new Set()
  return value.options.every(option => {
    if (!object(option) || !nonempty(option.option_key) || !["standardized", "raw"].includes(option.filter_type)
        || !string(option.label) || !string(option.unit) || !nullableString(option.property_key)
        || !nullableString(option.property_name) || !count(option.measurement_count)
        || !count(option.unique_smiles_count) || seen.has(option.option_key)) return false
    seen.add(option.option_key)
    const stats = [option.min_value, option.p5_value, option.median_value, option.p95_value, option.max_value]
    if (!stats.every(nullableNumber)) return false
    const present = stats.filter(value => value != null)
    return present.every((value, index) => index === 0 || present[index - 1] <= value)
  })
}

function validateHistogram(value, input) {
  const hist = value.histogram
  return input.dataset_revision === value.dataset_revision && input.option_key === value.option_key
    && nonempty(value.option_key) && string(value.unit) && value.distribution_scope === "entire_dataset_for_option"
    && object(hist) && finite(hist.domain_min) && finite(hist.domain_max) && hist.domain_min <= hist.domain_max
    && finite(hist.domain_max - hist.domain_min)
    && ["p5_p95", "full_range"].includes(hist.domain_kind) && positive(hist.bin_count) && hist.bin_count <= 40
    && Array.isArray(hist.counts) && hist.counts.length === hist.bin_count && hist.counts.every(count)
    && count(hist.underflow_count) && count(hist.overflow_count) && count(hist.total_count)
    && hist.counts.reduce((total, value) => total + value, hist.underflow_count + hist.overflow_count) === hist.total_count
}

function validateSearchInput(input, output) {
  return input.dataset_revision === output.dataset_revision && Array.isArray(input.filters)
    && input.filters.length > 0 && input.filters.length <= 8 && (input.q == null || string(input.q))
    && input.filters.every(filter => object(filter) && nonempty(filter.option_key)
      && nullableNumber(filter.min_value) && nullableNumber(filter.max_value)
      && (filter.min_value != null || filter.max_value != null)
      && (filter.min_value == null || filter.max_value == null || filter.min_value <= filter.max_value))
    && (input.page ?? 1) === output.page && (input.page_size ?? 10) === output.page_size
    && (input.measurement_limit_per_filter ?? 1) === output.measurement_limit_per_filter
}

function validateMeasurement(record, index, datasetRevision) {
  if (!object(record) || !nonempty(record.record_ref) || !count(record.filter_record_id)
      || !record.record_ref.startsWith(datasetRevision + ":")
      || !count(record.source_row_number) || record.filter_index !== index
      || !string(record.property_category) || !string(record.property_name) || !string(record.property_value)) return false
  for (const key of ["polymer_name", "smiles", "canonical_smiles", "property_unit_raw", "property_unit_clean",
    "property_key", "property_label", "canonical_unit", "unit_conversion_status", "value_origin", "label_source",
    "soft_quality_flags", "duplicate_flag"]) if (!nullableString(record[key])) return false
  return [record.property_value_num, record.canonical_value, record.reliable_score].every(nullableNumber)
}

function validateSearch(value, input) {
  if (!validatePage(value, 25) || !validateSearchInput(input, value) || !count(value.matched_material_count)
      || !positive(value.measurement_limit_per_filter) || value.measurement_limit_per_filter > 3
      || !Array.isArray(value.results) || value.results.length > value.page_size
      || value.results.length > value.matched_material_count || !string(value.semantics)) return false
  const materialRefs = new Set()
  return value.results.every(material => {
    if (!object(material) || !nonempty(material.material_ref) || !material.material_ref.startsWith(value.dataset_revision + ":") || materialRefs.has(material.material_ref)
        || ![material.polymer_name, material.smiles, material.canonical_smiles].every(nullableString)
        || material.matched_filters !== input.filters.length || !count(material.matching_evidence_count)
        || !Array.isArray(material.evidence) || material.evidence.length !== input.filters.length) return false
    materialRefs.add(material.material_ref)
    const indices = new Set()
    const valid = material.evidence.every(evidence => {
      if (!object(evidence) || !count(evidence.filter_index) || indices.has(evidence.filter_index)
          || evidence.option_key !== input.filters[evidence.filter_index]?.option_key || !string(evidence.unit)
          || !positive(evidence.matching_measurement_count) || typeof evidence.truncated !== "boolean"
          || !Array.isArray(evidence.measurements) || evidence.measurements.length < 1
          || evidence.measurements.length > value.measurement_limit_per_filter
          || evidence.measurements.length > evidence.matching_measurement_count
          || evidence.truncated !== (evidence.matching_measurement_count > evidence.measurements.length)) return false
      indices.add(evidence.filter_index)
      return evidence.measurements.every(record => validateMeasurement(record, evidence.filter_index, value.dataset_revision))
    })
    return valid && material.evidence.reduce((total, item) => total + item.matching_measurement_count, 0)
      === material.matching_evidence_count
  })
}

function sameUnits(group) {
  const units = new Map()
  const accept = (key, unit) => {
    if (units.has(key) && units.get(key) !== unit) return false
    units.set(key, unit)
    return true
  }
  for (const option of group.catalog) if (!accept(option.option_key, option.unit)) return false
  for (const hist of group.histograms) if (!accept(hist.option_key, hist.unit)) return false
  for (const material of group.search?.results ?? []) {
    for (const evidence of material.evidence) if (!accept(evidence.option_key, evidence.unit)) return false
  }
  return true
}

/**
 * Extract display models from the session GET /message array. Returns one model
 * per user message and dataset revision, in session order. Catalog-only groups
 * are omitted. The latest valid search for a revision supplies its conditions;
 * histograms describe the entire dataset and are deduplicated by property.
 *
 * Model: { key, turnId, datasetRevision, search, searchInput, catalog,
 *          histograms, sourceCalls }. Raw outputs are retained for provenance,
 * never inferred from model-written text. No network or DOM work occurs here.
 */
export function extractFilterTurns(messages) {
  if (!Array.isArray(messages)) return []
  const users = new Map()
  const rejected = new Set()
  for (const message of messages) {
    if (message?.info?.role === "user" && nonempty(message.info.id) && !users.has(message.info.id)) {
      users.set(message.info.id, new Map())
    }
  }
  for (const message of messages) {
    if (message?.info?.role !== "assistant" || !users.has(message.info.parentID) || !Array.isArray(message.parts)) continue
    const revisions = users.get(message.info.parentID)
    for (const part of message.parts) {
      if (part?.type !== "tool" || !string(part.tool) || !part.tool.startsWith(PREFIX)
          || !TOOLS.has(part.tool.slice(PREFIX.length)) || part.state?.status !== "completed"
          || !object(part.state.input)) continue
      const name = part.tool.slice(PREFIX.length)
      const value = structuredOutput(part)
      if (!value) {
        rejected.add(message.info.parentID)
        continue
      }
      const input = part.state.input
      if (!(name === "list_filter_properties" ? validateCatalog(value, input)
        : name === "search_polymers" ? validateSearch(value, input) : validateHistogram(value, input))) {
        rejected.add(message.info.parentID)
        continue
      }
      if (!revisions.has(value.dataset_revision)) revisions.set(value.dataset_revision, {
        key: `${message.info.parentID}:${value.dataset_revision}`,
        turnId: message.info.parentID,
        datasetRevision: value.dataset_revision,
        search: null,
        searchInput: null,
        catalog: [],
        histograms: [],
        sourceCalls: [],
      })
      const group = revisions.get(value.dataset_revision)
      if (name === "list_filter_properties") {
        for (const option of value.options) {
          const index = group.catalog.findIndex(previous => previous.option_key === option.option_key)
          if (index < 0) group.catalog.push(option)
          else group.catalog[index] = option
        }
      } else if (name === "search_polymers") {
        group.search = value
        group.searchInput = input
      } else {
        const index = group.histograms.findIndex(previous => previous.option_key === value.option_key)
        if (index < 0) group.histograms.push(value)
        else group.histograms[index] = value
      }
      group.sourceCalls.push({ kind: name, callId: part.callID ?? part.id ?? "", messageId: message.info.id ?? "" })
    }
  }
  const result = []
  for (const [turnId, revisions] of users) {
    const groups = [...revisions.values()].filter(group => group.search || group.histograms.length)
    const valid = groups.filter(sameUnits)
    if (groups.length !== valid.length) rejected.add(turnId)
    const issues = rejected.has(turnId) ? ["部分查询数据未通过完整性检查。请重新查询，以获取同一数据版本的完整结果。"] : []
    for (const group of valid) result.push({ ...group, status: "ready", issues })
    if (!valid.length && rejected.has(turnId)) result.push({
      key: `${turnId}:unavailable`, turnId, datasetRevision: null, status: "unavailable", issues,
      search: null, searchInput: null, catalog: [], histograms: [], sourceCalls: [],
    })
  }
  return result
}

function element(document, tag, className, text) {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text != null) node.textContent = String(text)
  return node
}

function append(parent, ...children) {
  for (const child of children) if (child) parent.append(child)
  return parent
}

function field(document, parent, title, content) {
  append(parent, element(document, "dt", "", title), element(document, "dd", "", content))
}

function propertyInfo(model, key, index) {
  const option = model.catalog.find(item => item.option_key === key)
  const evidence = model.search?.results.flatMap(material => material.evidence).find(item => item.option_key === key)
  const record = evidence?.measurements[0]
  const hist = model.histograms.find(item => item.option_key === key)
  return {
    label: option?.label?.trim() || record?.property_label?.trim() || record?.property_name?.trim()
      || (index == null ? "所选属性" : `条件 ${index + 1}`),
    unit: option?.unit ?? evidence?.unit ?? hist?.unit ?? "",
    type: option?.filter_type,
  }
}

function rangeText(filter, unit) {
  const suffix = unit ? ` ${unit}` : "（无单位）"
  if (filter.min_value != null && filter.max_value != null) {
    return `${number(filter.min_value)} – ${number(filter.max_value)}${suffix}（含边界）`
  }
  return `${filter.min_value != null ? "≥ " + number(filter.min_value) : "≤ " + number(filter.max_value)}${suffix}`
}

function renderOverview(document, model) {
  const box = element(document, "section", "nexpoly-overview")
  box.dataset.resultSummary = ""
  append(box, element(document, "h3", "nexpoly-section-title", "查询概览"))
  if (model.search) {
    const stats = element(document, "dl", "nexpoly-stats")
    field(document, stats, "命中结构组数", number(model.search.matched_material_count))
    field(document, stats, "本页数量", `${number(model.search.results.length)} 个结构组`)
    field(document, stats, "当前页", `第 ${number(model.search.page)} 页${model.search.has_more ? " · 还有更多结果" : ""}`)
    append(box, stats)
    const conditions = element(document, "ul", "nexpoly-conditions")
    for (const [index, filter] of model.searchInput.filters.entries()) {
      const info = propertyInfo(model, filter.option_key, index)
      const item = element(document, "li", "")
      append(item, element(document, "strong", "", info.label), element(document, "span", "", rangeText(filter, info.unit)))
      append(conditions, item)
    }
    append(box, element(document, "p", "nexpoly-eyebrow", "筛选条件 · 同时满足"), conditions)
    if (model.searchInput.q?.trim()) append(box, element(document, "p", "nexpoly-note", `名称 / SMILES 关键词：${model.searchInput.q}`))
  } else append(box, element(document, "p", "nexpoly-note", "本次已获取属性分布，尚无材料筛选结果。"))
  append(box, element(document, "p", "nexpoly-semantics", "测量口径：按规范化 SMILES 归为结构组；缺失时使用原始 SMILES，再缺失时按记录区分。不同条件可能由不同样本或实验满足。数值可能包含实测、中位数与填补值；按所选属性的指定单位筛选，检索时不做单位换算。"))
  return box
}

const ORIGIN = { observed: "实测值", median: "中位数值", imputed: "填补值" }
const LABEL_SOURCE = { exp: "实验数据", experimental: "实验数据", computed: "计算数据", predicted: "预测数据" }
const CONVERSION = { already_standard: "已使用标准单位", converted: "已换算", not_convertible: "无法换算",
  no_conversion_needed: "无需换算", missing_unit: "缺少单位", unknown_unit: "单位未识别" }
const qualityText = value => !value || value === "none" ? "无标记" : value

function matchedValue(record, evidence, info) {
  if (info.type === "raw") return record.property_value_num
  if (info.type === "standardized") return record.canonical_value
  if (record.canonical_unit === evidence.unit && record.canonical_value != null) return record.canonical_value
  if (record.property_unit_clean === evidence.unit) return record.property_value_num
  return null
}

function renderMeasurement(document, record, evidence, info) {
  const item = element(document, "li", "nexpoly-measurement")
  item.dataset.measurement = ""
  item.dataset.recordRef = record.record_ref
  const value = matchedValue(record, evidence, info)
  const header = element(document, "div", "nexpoly-measurement-head")
  append(header, element(document, "strong", "nexpoly-value", value == null ? "未提供匹配数值" : `${number(value)}${evidence.unit ? " " + evidence.unit : "（无单位）"}`),
    element(document, "span", "nexpoly-badge", ORIGIN[record.value_origin] ?? (record.value_origin || "来源类型未提供")))
  const details = element(document, "dl", "nexpoly-measurement-data")
  field(document, details, "原始值", `${record.property_value}${record.property_unit_raw ? " " + record.property_unit_raw : ""}`)
  field(document, details, "标准化值", record.canonical_value == null ? "未提供"
    : `${number(record.canonical_value)}${record.canonical_unit ? " " + record.canonical_unit : "（无单位）"}`)
  field(document, details, "单位处理", CONVERSION[record.unit_conversion_status] ?? record.unit_conversion_status ?? "未提供")
  field(document, details, "来源类型", LABEL_SOURCE[record.label_source] ?? record.label_source ?? "未提供")
  const quality = [`质量标记：${qualityText(record.soft_quality_flags)}`, `重复标记：${qualityText(record.duplicate_flag)}`]
  if (record.reliable_score != null) quality.push(`可靠性分数：${number(record.reliable_score)}`)
  field(document, details, "质量信息", quality.join("；"))
  return append(item, header, details)
}

function renderMaterial(document, material, index, model) {
  const card = element(document, "article", "nexpoly-material")
  card.dataset.materialCard = ""
  card.dataset.materialRef = material.material_ref
  const header = element(document, "header", "nexpoly-material-head")
  append(header, element(document, "span", "nexpoly-material-index", String(index + 1).padStart(2, "0")),
    element(document, "h4", "", material.polymer_name?.trim() || "未提供材料名称"))
  append(card, header)
  const structure = element(document, "div", "nexpoly-structure")
  append(structure, element(document, "p", "nexpoly-eyebrow", "结构 · SMILES"))
  const smiles = material.canonical_smiles || material.smiles
  const code = element(document, "code", "nexpoly-smiles", smiles || "该记录未提供 SMILES")
  code.dataset.smiles = ""
  append(card, append(structure, code), element(document, "p", "nexpoly-note",
    `${number(material.matched_filters)} 项条件均命中 · ${number(material.matching_evidence_count)} 条按条件计数的匹配测量（同条测量可重复计入不同条件）`))
  for (const evidence of [...material.evidence].sort((a, b) => a.filter_index - b.filter_index)) {
    const info = propertyInfo(model, evidence.option_key, evidence.filter_index)
    const section = element(document, "section", "nexpoly-evidence")
    section.dataset.filterEvidence = ""
    append(section, element(document, "h5", "", `${info.label} · ${rangeText(model.searchInput.filters[evidence.filter_index], evidence.unit)}`))
    const list = element(document, "ol", "nexpoly-measurements")
    for (const record of evidence.measurements) append(list, renderMeasurement(document, record, evidence, info))
    const omitted = evidence.matching_measurement_count - evidence.measurements.length
    const note = element(document, "p", "nexpoly-evidence-count", `匹配 ${number(evidence.matching_measurement_count)} 条测量，展示 ${number(evidence.measurements.length)} 条${evidence.truncated ? `；另 ${number(omitted)} 条未展示（证据已截断）` : "；证据已全部展示"}。`)
    note.dataset.evidenceTruncation = String(evidence.truncated)
    append(card, append(section, list, note))
  }
  return card
}

function svg(document, tag, attributes, text) {
  const node = document.createElementNS(NS, tag)
  for (const [key, value] of Object.entries(attributes ?? {})) node.setAttribute(key, String(value))
  if (text != null) node.textContent = String(text)
  return node
}

function renderHistogram(document, output, model, index) {
  const info = propertyInfo(model, output.option_key)
  const hist = output.histogram
  const figure = element(document, "figure", "nexpoly-histogram")
  figure.dataset.propertyHistogram = ""
  const caption = element(document, "figcaption", "")
  append(caption, element(document, "h4", "", `${info.label} · 全库测量分布`),
    element(document, "p", "nexpoly-note", `该属性在整个数据库中的测量分布，未应用本次材料筛选条件。样本数 ${number(hist.total_count)} 条测量。`))
  append(figure, caption)
  const summary = `显示范围 ${number(hist.domain_min)} – ${number(hist.domain_max)}${output.unit ? " " + output.unit : "（无单位）"}（${hist.domain_kind === "p5_p95" ? "第 5–95 百分位" : "完整数值范围"}）；低于范围 ${number(hist.underflow_count)} 条，高于范围 ${number(hist.overflow_count)} 条。`
  const encodedKey = Array.from(model.key, character => character.codePointAt(0).toString(16)).join("-")
  const descriptionId = `nexpoly-hist-${encodedKey}-${index}`
  const chart = svg(document, "svg", { viewBox: "0 0 760 284", role: "img", "aria-labelledby": `${descriptionId}-title ${descriptionId}-desc`, "data-histogram-chart": "" })
  append(chart, svg(document, "title", { id: `${descriptionId}-title` }, `${info.label}的全库测量直方图`),
    svg(document, "desc", { id: `${descriptionId}-desc` }, `${summary}共 ${hist.bin_count} 个等宽区间，柱高依次为 ${hist.counts.map(number).join("、")} 条测量。`))
  const left = 66, top = 24, width = 664, height = 182
  const largest = Math.max(1, ...hist.counts)
  const tickStep = Math.max(1, Math.ceil(largest / 4))
  const maximum = tickStep * 4
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = tick * tickStep
    const y = top + height - value / maximum * height
    append(chart, svg(document, "line", { x1: left, x2: left + width, y1: y, y2: y, class: "nexpoly-gridline" }),
      svg(document, "text", { x: left - 10, y: y + 4, "text-anchor": "end", class: "nexpoly-axis-text" }, number(value)))
  }
  append(chart, svg(document, "text", { x: left, y: 13, class: "nexpoly-axis-text" }, "测量数（条）"))
  const step = width / hist.bin_count
  const binWidth = (hist.domain_max - hist.domain_min) / hist.bin_count
  const tooltip = element(document, "p", "nexpoly-histogram-tooltip", "悬停柱形或用 Tab 聚焦，查看区间与测量数。")
  tooltip.dataset.histogramTooltip = ""
  tooltip.setAttribute("role", "status")
  tooltip.setAttribute("aria-live", "polite")
  const showTooltip = label => { tooltip.textContent = label }
  for (const [binIndex, value] of hist.counts.entries()) {
    const lower = hist.domain_min + binIndex * binWidth
    const upper = binIndex === hist.bin_count - 1 ? hist.domain_max : hist.domain_min + (binIndex + 1) * binWidth
    const label = `${number(lower)} 至 ${number(upper)}${output.unit ? " " + output.unit : ""}，${number(value)} 条测量${binIndex === hist.bin_count - 1 ? "（含右端点）" : "（不含右端点）"}`
    const barHeight = value / maximum * height
    const bar = svg(document, "rect", { x: left + binIndex * step + 1.5, y: top + height - barHeight,
      width: Math.max(1, step - 3), height: barHeight, rx: 2, class: "nexpoly-histogram-bar",
      tabindex: 0, role: "graphics-symbol", "aria-label": label, "data-histogram-bin": "", "data-count": value, "data-bin-index": binIndex })
    append(bar, svg(document, "title", {}, label))
    bar.addEventListener("pointerenter", () => showTooltip(label))
    bar.addEventListener("focus", () => showTooltip(label))
    append(chart, bar)
  }
  for (let tick = 0; tick <= 4; tick += 1) {
    const x = left + tick * width / 4
    const value = hist.domain_min + tick * (hist.domain_max - hist.domain_min) / 4
    append(chart, svg(document, "text", { x, y: top + height + 25,
      "text-anchor": tick === 0 ? "start" : tick === 4 ? "end" : "middle", class: "nexpoly-axis-text" }, number(value)))
  }
  append(chart, svg(document, "text", { x: left + width / 2, y: 267, "text-anchor": "middle", class: "nexpoly-axis-text" }, output.unit || "属性值（无单位）"))
  const scroller = element(document, "div", "nexpoly-chart-scroll")
  const note = element(document, "p", "nexpoly-chart-summary", summary)
  note.dataset.histogramSummary = ""
  return append(figure, append(scroller, chart), tooltip, note)
}

function renderProvenance(document, model) {
  const details = element(document, "details", "nexpoly-provenance")
  details.dataset.resultProvenance = ""
  append(details, element(document, "summary", "", "数据版本与来源细节"))
  const list = element(document, "dl", "nexpoly-provenance-data")
  field(document, list, "数据版本", model.datasetRevision)
  field(document, list, "数据状态", (model.search?.source_status ?? model.histograms[0]?.source_status) === "empty" ? "当前数据集为空" : "可用")
  if (model.search) field(document, list, "全库测量记录数", number(model.search.total_measurement_count))
  field(document, list, "证据计数口径", "结构按 SMILES 归组；同一测量可同时满足多个条件，按条件分别计数。")
  append(details, list)
  for (const [index, material] of (model.search?.results ?? []).entries()) {
    const group = element(document, "section", "nexpoly-source-group")
    append(group, element(document, "h4", "", `材料 ${index + 1} · 来源记录`))
    const sourceList = element(document, "dl", "nexpoly-provenance-data")
    field(document, sourceList, "结构组标识", material.material_ref)
    if (material.smiles && material.smiles !== material.canonical_smiles) field(document, sourceList, "原始 SMILES", material.smiles)
    for (const evidence of material.evidence) {
      const info = propertyInfo(model, evidence.option_key, evidence.filter_index)
      for (const record of evidence.measurements) {
        field(document, sourceList, info.label, `记录 ${record.record_ref} · 来源行 ${record.source_row_number} · 原始属性：${record.property_name}`)
      }
    }
    append(details, append(group, sourceList))
  }
  return details
}

/** Render an extracted model using text nodes and SVG attributes only. */
export function renderFilterResult(document, model) {
  if (!document?.createElement || !object(model) || !nonempty(model.turnId)
      || (model.status !== "unavailable" && (!revision(model.datasetRevision)
        || !Array.isArray(model.catalog) || !Array.isArray(model.histograms) || (!model.search && !model.histograms.length)))) {
    throw new TypeError("A document and an extracted filter-result model are required")
  }
  const root = element(document, "section", "nexpoly-results")
  root.dataset.nexpolyResults = ""
  root.dataset.turnId = model.turnId
  if (model.datasetRevision) root.dataset.datasetRevision = model.datasetRevision
  root.setAttribute("aria-label", "聚合物筛选结果")
  const header = element(document, "header", "nexpoly-results-header")
  append(header, element(document, "p", "nexpoly-eyebrow", "NEXPOLY · 数据库查询"),
    element(document, "h2", "", "聚合物筛选结果"))
  append(root, header)
  for (const issue of model.issues ?? []) append(root, element(document, "p", "nexpoly-result-issue", issue))
  if (model.status === "unavailable") return root
  append(root, renderOverview(document, model))
  if (model.search) {
    const materials = element(document, "section", "nexpoly-materials")
    append(materials, element(document, "h3", "nexpoly-section-title", "匹配材料"))
    if (!model.search.results.length) append(materials, element(document, "p", "nexpoly-empty", "当前条件未匹配到结构组。可调整数值范围或减少筛选条件后重新查询。"))
    for (const [index, material] of model.search.results.entries()) append(materials, renderMaterial(document, material, index, model))
    append(root, materials)
  }
  if (model.histograms.length) {
    const distribution = element(document, "section", "nexpoly-distributions")
    append(distribution, element(document, "h3", "nexpoly-section-title", "属性分布"))
    for (const [index, histogram] of model.histograms.entries()) append(distribution, renderHistogram(document, histogram, model, index))
    append(root, distribution)
  }
  return append(root, renderProvenance(document, model))
}
