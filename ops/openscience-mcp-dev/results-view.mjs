// Development-only presentation of authoritative property-filter tool outputs.
// This module is deliberately independent of the model's prose and of the host UI.
// Keep persisted direct-MCP history readable after routing new queries through
// the development capability gateway. Other gateway tools are not filter data.
const TOOLS = new Map(["nexpoly_property_filter_", "capability_gateway_"].flatMap(prefix =>
  ["list_filter_properties", "search_polymers", "get_property_histogram"].map(name => [prefix + name, name])))
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

function successfulOutput(part) {
  if (!string(part.state?.output)) return null
  const output = part.state.output.trim()
  // The adapter appends structuredContent on the final line after its summary.
  // Never search prose or the assistant's final answer for a convenient object.
  const candidates = [output, output.slice(output.lastIndexOf("\n") + 1).trim()]
  for (const candidate of candidates) {
    if (!candidate.startsWith("{")) continue
    try {
      const result = JSON.parse(candidate)
      if (object(result) && result.ok === true && !Object.hasOwn(result, "error")) return result
    } catch { /* An incomplete or unsuccessful tool output is not display data. */ }
  }
  return null
}

function structuredOutput(part) {
  const result = successfulOutput(part)
  return result && revision(result.dataset_revision) && ["ready", "empty"].includes(result.source_status) ? result : null
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
      const name = TOOLS.get(part?.tool)
      if (part?.type !== "tool" || !name || part.state?.status !== "completed"
          || !object(part.state.input)) continue
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

const POLYTAO_TOOLS = new Map([
  ...["submit_polytao_generation", "get_polytao_job", "prepare_polytao_generation", "get_polytao_draft"]
    .map(name => ["nexpoly_polytao_" + name, name]),
  ...["get_polytao_job", "prepare_polytao_generation", "get_polytao_draft"]
    .map(name => ["capability_gateway_" + name, name]),
])
const POLYTAO_STATES = new Set(["pending", "submitted", "running", "completed", "failed", "cancelled"])
const POLYTAO_JOB_TOOLS = new Set(["submit_polytao_generation", "get_polytao_job"])
const strings = value => Array.isArray(value) && value.every(string)
const timestamp = value => nonempty(value) && Number.isFinite(Date.parse(value))
const optionalTimestamp = value => value == null || timestamp(value)

function validatePolytaoSnapshot(value, input) {
  if (input.job_id !== value.job_id || !nonempty(value.job_id) || !POLYTAO_STATES.has(value.status)
      || !timestamp(value.snapshot_at) || !timestamp(value.created_at) || !timestamp(value.updated_at)
      || !optionalTimestamp(value.started_at) || !optionalTimestamp(value.finished_at)
      || !nullableString(value.progress_stage) || !(value.progress_percent == null
        || finite(value.progress_percent) && value.progress_percent >= 0 && value.progress_percent <= 100)
      || !positive(value.requested_count) || value.requested_count > 50 || !count(value.returned_count)
      || value.returned_count > value.requested_count || !count(value.attempts)
      || !object(value.filter_counter) || !Object.values(value.filter_counter).every(count)
      || !strings(value.warnings) || !nullableString(value.error_code)
      || !count(value.offset) || !positive(value.limit) || value.limit > 25
      || value.offset !== (input.offset ?? 0) || value.limit !== (input.limit ?? 10)
      || !Array.isArray(value.results) || value.results.length > value.limit
      || value.results.length > Math.max(0, value.returned_count - value.offset)) return false
  const expectedNext = value.offset + value.results.length < value.returned_count
    ? value.offset + value.results.length : null
  if (value.next_offset !== expectedNext || expectedNext != null && !value.results.length) return false
  return value.results.every((item, index) => object(item) && item.rank === value.offset + index + 1
    && nonempty(item.generated_smiles) && typeof item.valid_smiles === "boolean"
    && nullableNumber(item.sa_score) && (item.sa_score == null || item.sa_score >= 0) && strings(item.warnings))
}

/** Passive, turn-local projection of exact PolyTAO tool results. Never submits or polls jobs. */
export function extractPolytaoTurns(messages) {
  if (!Array.isArray(messages)) return []
  const users = new Map(messages.filter(message => message?.info?.role === "user" && nonempty(message.info.id))
    .map(message => [message.info.id, new Map()]))
  for (const message of messages) {
    if (message?.info?.role !== "assistant" || !users.has(message.info.parentID) || !Array.isArray(message.parts)) continue
    const jobs = users.get(message.info.parentID)
    for (const part of message.parts) {
      if (part?.type !== "tool" || !POLYTAO_JOB_TOOLS.has(POLYTAO_TOOLS.get(part.tool))
          || part.state?.status !== "completed" || !object(part.state.input)) continue
      const name = POLYTAO_TOOLS.get(part.tool)
      const value = successfulOutput(part)
      const input = part.state.input
      const isSnapshot = name === "get_polytao_job"
      const valid = value && (isSnapshot ? validatePolytaoSnapshot(value, input)
        : nonempty(input.idempotency_key) && object(input.descriptors) && nonempty(value.job_id)
          && POLYTAO_STATES.has(value.status) && typeof value.replayed === "boolean"
          && object(value.effective_parameters) && positive(value.effective_parameters.candidate_count)
          && value.effective_parameters.candidate_count <= 50)
      if (!valid) continue
      if (!jobs.has(value.job_id)) jobs.set(value.job_id, {
        kind: "polytao", key: `polytao:${message.info.parentID}:${value.job_id}`,
        turnId: message.info.parentID, jobId: value.job_id, status: value.status,
        requestedCount: isSnapshot ? value.requested_count : value.effective_parameters.candidate_count,
        snapshot: null, results: [], sourceCalls: [], issues: [],
      })
      const model = jobs.get(value.job_id)
      model.sourceCalls.push({ kind: name, callId: part.callID ?? part.id ?? "", messageId: message.info.id ?? "" })
      if (!isSnapshot) continue
      const previous = model.snapshot
      // Saved message lists can arrive out of order. Older pages may only extend
      // an immutable completed result with the same result count and finish time.
      const older = previous && Date.parse(value.snapshot_at) < Date.parse(previous.snapshot_at)
      const compatible = !previous || previous.status === value.status && previous.returned_count === value.returned_count
        && previous.finished_at === value.finished_at
      if (older && !(compatible && value.status === "completed")) continue
      if (!compatible) model.results = []
      const candidates = new Map(model.results.map(item => [item.rank, item]))
      let conflict = false
      for (const candidate of value.results) {
        if (candidates.has(candidate.rank) && JSON.stringify(candidates.get(candidate.rank)) !== JSON.stringify(candidate)) {
          conflict = true
          break
        }
        candidates.set(candidate.rank, candidate)
      }
      if (conflict) {
        model.issues = ["候选分页内容不一致，请重新查询该任务。"]
        continue
      }
      model.results = [...candidates.values()].sort((a, b) => a.rank - b.rank)
      if (!older) {
        model.snapshot = value
        model.status = value.status
        model.requestedCount = value.requested_count
      }
    }
  }
  return [...users.values()].flatMap(jobs => [...jobs.values()])
}

const POLYTAO_STATUS_LABEL = { pending: "等待调度", submitted: "已提交", running: "运行中",
  completed: "已完成", failed: "失败", cancelled: "已取消" }

export function renderPolytaoResult(document, model) {
  if (!document?.createElement || model?.kind !== "polytao" || !nonempty(model.turnId) || !nonempty(model.jobId)) {
    throw new TypeError("An extracted PolyTAO result is required")
  }
  const root = element(document, "section", "nexpoly-results nexpoly-polytao")
  root.dataset.nexpolyResults = ""
  root.dataset.polytaoResults = ""
  root.dataset.turnId = model.turnId
  root.dataset.jobId = model.jobId
  root.dataset.nexpolyPolytaoJob = model.jobId
  root.setAttribute("aria-label", "聚合物生成结果")
  const header = element(document, "header", "nexpoly-results-header")
  append(header, element(document, "p", "nexpoly-eyebrow", "NEXPOLY · POLYTAO · 开发验证账号"),
    element(document, "h2", "", "聚合物生成结果"))
  const summary = element(document, "dl", "nexpoly-polytao-summary")
  for (const [label, value] of [
    ["任务状态", POLYTAO_STATUS_LABEL[model.status] ?? model.status],
    ["已返回 / 请求候选", `${model.snapshot?.returned_count ?? "待确认"} / ${model.requestedCount}`],
  ]) {
    const metric = element(document, "div", "nexpoly-polytao-metric")
    field(document, metric, label, value)
    append(summary, metric)
  }
  append(root, header, summary)
  if (model.snapshot) {
    const updated = element(document, "p", "nexpoly-polytao-updated")
    const time = element(document, "time", "", new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "short", timeStyle: "long",
    }).format(new Date(model.snapshot.snapshot_at)))
    time.setAttribute("datetime", model.snapshot.snapshot_at)
    time.setAttribute("title", model.snapshot.snapshot_at)
    append(root, append(updated, element(document, "span", "", "查询时间："), time))
  }
  if (["pending", "submitted", "running"].includes(model.status)) {
    append(root, element(document, "p", "nexpoly-note", "以上为最近一次查询的状态，可在对话中继续询问任务进度。"))
  }
  append(root, element(document, "p", "nexpoly-semantics",
    "候选按接受顺序排列。SA score 用于评估合成难度，不代表成功概率；结构可解析不保证可合成或精确满足目标描述符。"))
  for (const issue of model.issues) append(root, element(document, "p", "nexpoly-result-issue", issue))
  for (const warning of model.snapshot?.warnings ?? []) append(root, element(document, "p", "nexpoly-result-issue", warning))
  if (model.snapshot?.error_code) append(root, element(document, "p", "nexpoly-result-issue", `任务错误：${model.snapshot.error_code}`))
  if (model.status === "completed" && model.snapshot) {
    if (model.snapshot.returned_count === 0) append(root, element(document, "p", "nexpoly-empty", "任务已完成，本次没有通过过滤的候选。"))
    else if (model.snapshot.returned_count < model.requestedCount) append(root, element(document, "p", "nexpoly-note", "本次返回部分候选，未达到请求数量。"))
  }
  if (!model.snapshot) append(root, element(document, "p", "nexpoly-note", "生成请求已接受，候选结果尚未读取；可在对话中继续查询该任务。"))
  if (model.snapshot && model.results.length < model.snapshot.returned_count) {
    append(root, element(document, "p", "nexpoly-note", `已载入 ${model.results.length} / ${model.snapshot.returned_count} 个候选；可在对话中继续查询其他分页。`))
  }
  for (const candidate of model.results) {
    const card = element(document, "article", "nexpoly-material")
    card.dataset.polytaoCandidate = ""
    card.dataset.rank = candidate.rank
    card.dataset.saScore = candidate.sa_score == null ? "" : String(candidate.sa_score)
    append(card, element(document, "h3", "nexpoly-section-title", `候选 ${candidate.rank}`))
    const smiles = element(document, "code", "nexpoly-smiles", candidate.generated_smiles)
    smiles.dataset.smiles = ""
    const copyButton = element(document, "button", "nexpoly-copy-smiles", "复制 SMILES")
    copyButton.type = "button"
    copyButton.setAttribute("aria-label", `复制候选 ${candidate.rank} 的 SMILES`)
    const feedback = element(document, "span", "nexpoly-copy-status", "")
    feedback.setAttribute("role", "status")
    copyButton.addEventListener("click", async () => {
      try {
        await document.defaultView.navigator.clipboard.writeText(candidate.generated_smiles)
        feedback.textContent = "已复制"
      } catch { feedback.textContent = "复制失败，请选中 SMILES 手动复制。" }
    })
    append(card, smiles, copyButton, feedback,
      element(document, "p", "nexpoly-note", `SA score：${candidate.sa_score == null ? "未提供" : number(candidate.sa_score)} · 结构解析：${candidate.valid_smiles ? "有效" : "无效"}`))
    for (const warning of candidate.warnings) append(card, element(document, "p", "nexpoly-result-issue", warning))
    append(root, card)
  }
  const details = element(document, "details", "nexpoly-provenance")
  append(details, element(document, "summary", "", "任务详情"))
  const task = element(document, "dl", "nexpoly-provenance-data")
  field(document, task, "任务编号", model.jobId)
  if (model.snapshot?.progress_stage) {
    field(document, task, "执行阶段", POLYTAO_STATUS_LABEL[model.snapshot.progress_stage] ?? model.snapshot.progress_stage)
  }
  append(root, append(details, task))
  return root
}

const POLYTAO_DRAFT_TOOLS = new Set(["prepare_polytao_generation", "get_polytao_draft"])
const POLYTAO_DRAFT_STATES = new Set(["editing", "confirmed", "submitted", "submission_unknown", "rejected", "cancelled"])
const POLYTAO_PARAMETERS = ["candidate_count", "temperature", "top_k", "top_p", "max_length"]
const DRAFT_ID = /^ptd_[A-Za-z0-9_-]{8,100}$/
const FIELD_NAME = /^[A-Za-z][A-Za-z0-9_]{0,79}$/
const own = (value, key) => Object.hasOwn(value, key)

/** A draft is displayable only with its complete MCP-provided field definition. */
export function validPolytaoDraft(value) {
  if (!object(value) || value.ok !== true || !DRAFT_ID.test(value.draft_id ?? "")
      || !/^ses_[A-Za-z0-9_-]+$/.test(value.conversation_id ?? "") || !positive(value.revision)
      || !POLYTAO_DRAFT_STATES.has(value.status) || typeof value.ready !== "boolean"
      || typeof value.editable !== "boolean" || !object(value.descriptors) || !object(value.descriptor_sources)
      || !object(value.parameters) || !object(value.parameter_fields) || !Array.isArray(value.descriptor_fields)
      || value.descriptor_fields.length !== 15 || !strings(value.missing_fields) || !Array.isArray(value.errors)
      || !timestamp(value.created_at) || !timestamp(value.updated_at)
      || !nullableString(value.reference_smiles) || !nullableString(value.canonical_smiles)
      || !nullableString(value.job_id) || !nullableString(value.submission_error)) return false
  const names = new Set()
  for (const field of value.descriptor_fields) {
    if (!object(field) || !FIELD_NAME.test(field.name ?? "") || names.has(field.name)
        || !nonempty(field.label) || !string(field.description) || !nullableString(field.unit)
        || field.required !== true) return false
    names.add(field.name)
  }
  if (!Object.entries(value.descriptors).every(([key, number]) => names.has(key) && finite(number))
      || !Object.entries(value.descriptor_sources).every(([key, source]) => names.has(key)
        && own(value.descriptors, key) && ["user", "reference"].includes(source))
      || !value.missing_fields.every(name => names.has(name) && !own(value.descriptors, name))
      || names.size - Object.keys(value.descriptors).length !== value.missing_fields.length
      || new Set(value.missing_fields).size !== value.missing_fields.length
      || !value.errors.every(error => object(error) && nonempty(error.field) && string(error.message))) return false
  if (!POLYTAO_PARAMETERS.every(name => finite(value.parameters[name]) && object(value.parameter_fields[name])
      && ["integer", "number"].includes(value.parameter_fields[name].type)
      && finite(value.parameter_fields[name].default))) return false
  return Object.keys(value.parameters).length === POLYTAO_PARAMETERS.length
    && Object.keys(value.parameter_fields).length === POLYTAO_PARAMETERS.length
}

/** One persistent panel per draft, anchored to its first genuine tool turn. */
export function extractPolytaoDrafts(messages) {
  if (!Array.isArray(messages)) return []
  const users = new Set(messages.filter(message => message?.info?.role === "user" && nonempty(message.info.id))
    .map(message => message.info.id))
  const drafts = new Map()
  for (const message of messages) {
    if (message?.info?.role !== "assistant" || !users.has(message.info.parentID) || !Array.isArray(message.parts)) continue
    for (const part of message.parts) {
      if (part?.type !== "tool" || !POLYTAO_DRAFT_TOOLS.has(POLYTAO_TOOLS.get(part.tool))
          || part.state?.status !== "completed" || !object(part.state.input)) continue
      const draft = successfulOutput(part)
      if (!validPolytaoDraft(draft) || part.state.input.draft_id != null && part.state.input.draft_id !== draft.draft_id
          || part.state.input.conversation_id != null && part.state.input.conversation_id !== draft.conversation_id) continue
      const previous = drafts.get(draft.draft_id)
      if (previous && (previous.draft.conversation_id !== draft.conversation_id || previous.draft.revision > draft.revision
        || previous.draft.revision === draft.revision && Date.parse(previous.draft.updated_at) > Date.parse(draft.updated_at))) continue
      if (!previous) drafts.set(draft.draft_id, { kind: "polytao-draft", key: `polytao-draft:${draft.draft_id}`,
        turnId: message.info.parentID, draft, sourceCalls: [] })
      const model = drafts.get(draft.draft_id)
      model.draft = draft
      model.sourceCalls.push({ kind: POLYTAO_TOOLS.get(part.tool),
        callId: part.callID ?? part.id ?? "", messageId: message.info.id ?? "" })
    }
  }
  return [...drafts.values()]
}

/** Preserve blanks as missing values; never coerce an empty input to zero. */
export function polytaoNumericInput(text, schema = {}) {
  const source = String(text).trim()
  if (!source) return { value: null, error: null }
  if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(source)) return { value: null, error: "请输入有效数值。" }
  const value = Number(source)
  if (!Number.isFinite(value)) return { value: null, error: "请输入有限数值。" }
  if (schema.type === "integer" && !Number.isSafeInteger(value)) return { value: null, error: "请输入整数。" }
  if (finite(schema.minimum) && value < schema.minimum) return { value: null, error: `数值不能小于 ${schema.minimum}。` }
  if (finite(schema.maximum) && value > schema.maximum) return { value: null, error: `数值不能大于 ${schema.maximum}。` }
  if (finite(schema.exclusiveMinimum) && value <= schema.exclusiveMinimum) return { value: null, error: `数值必须大于 ${schema.exclusiveMinimum}。` }
  if (finite(schema.exclusiveMaximum) && value >= schema.exclusiveMaximum) return { value: null, error: `数值必须小于 ${schema.exclusiveMaximum}。` }
  return { value, error: null }
}

const DESCRIPTOR_GROUPS = [
  { label: "分子规模与组成", names: ["MolWt", "HeavyAtomCount", "NOCount", "NumHeteroatoms"] },
  { label: "氢键与柔性", names: ["NHOHCount", "NumHAcceptors", "NumHDonors", "NumRotatableBonds"] },
  { label: "环结构", names: ["RingCount", "NumAliphaticCarbocycles", "NumAliphaticHeterocycles", "NumAliphaticRings", "NumAromaticCarbocycles", "NumAromaticHeterocycles", "NumAromaticRings"] },
]
const DESCRIPTOR_HELP = {
  MolWt: "目标结构的分子量，单位为 g/mol。", HeavyAtomCount: "除氢以外的原子数量。",
  NHOHCount: "结构中 NH 和 OH 基团的计数。", NOCount: "氮原子与氧原子的总数。",
  NumHAcceptors: "能够接受氢键的位点数量。", NumHDonors: "能够提供氢键的位点数量。",
  NumHeteroatoms: "杂原子的数量。", NumRotatableBonds: "可旋转化学键的数量，用于描述结构柔性。",
  RingCount: "所有环的总数。", NumAliphaticCarbocycles: "非芳香碳环的数量。",
  NumAliphaticHeterocycles: "非芳香杂环的数量。", NumAliphaticRings: "非芳香环的总数。",
  NumAromaticCarbocycles: "芳香碳环的数量。", NumAromaticHeterocycles: "芳香杂环的数量。",
  NumAromaticRings: "芳香环的总数。",
}
const PARAMETER_LABELS = { candidate_count: "候选数量", temperature: "采样温度", top_k: "候选词数量（Top K）",
  top_p: "累计概率阈值（Top P）", max_length: "最大生成长度" }
const PARAMETER_HELP = { candidate_count: "本次期望获得的候选数，实际可能少于请求数量。",
  temperature: "数值越高，采样通常越多样。", top_k: "每步从概率最高的指定数量词元中采样。",
  top_p: "按此累计概率范围保留可采样词元。", max_length: "生成序列的词元长度上限，不是原子数。" }
const DRAFT_STATUS_LABEL = { editing: "待确认", confirmed: "正在提交", submitted: "已提交生成任务",
  submission_unknown: "提交结果待核查", rejected: "提交未被接受", cancelled: "草稿已取消" }
const BRIDGE_ERROR_LABEL = {
  REVISION_CONFLICT: "草稿已更新，请重新载入已保存参数后再操作。",
  DRAFT_REVISION_CONFLICT: "草稿已更新，请重新载入已保存参数后再操作。",
  DRAFT_NOT_EDITABLE: "该草稿已结束编辑，请重新载入查看最新状态。",
  SESSION_BUSY: "当前对话仍在处理中，请稍后继续。",
}

/**
 * Stateful form over authoritative MCP drafts. The only transport is a supplied
 * same-origin server bridge; confirmation and continuation remain on the server.
 */
export function renderPolytaoDraft(document, model, actions) {
  if (!document?.createElement || model?.kind !== "polytao-draft" || !validPolytaoDraft(model.draft)
      || typeof actions?.perform !== "function") throw new TypeError("An extracted draft and its session bridge are required")
  let draft = model.draft
  let busy = false
  let dirty = false
  let syncing = true
  let stale = false
  let noticeText = "正在恢复最新草稿……"
  let continuation = null
  const fields = new Map()
  const groups = []
  const root = element(document, "section", "nexpoly-results nexpoly-polytao-form")
  root.dataset.nexpolyResults = ""
  root.dataset.polytaoDraft = draft.draft_id
  root.dataset.turnId = model.turnId
  root.setAttribute("aria-label", "聚合物生成参数")
  const header = element(document, "header", "nexpoly-results-header")
  append(root, append(header, element(document, "p", "nexpoly-eyebrow", "NEXPOLY · POLYTAO"),
    element(document, "h2", "", "聚合物生成参数")))
  append(root, element(document, "p", "nexpoly-note", "填写或补齐 15 项目标描述符，再点击“确认并生成”。描述符没有默认值；保存参数和计算描述符均不会启动生成任务。"))
  const status = element(document, "p", "nexpoly-draft-status")
  status.dataset.polytaoDraftStatus = ""
  status.setAttribute("role", "status")
  status.setAttribute("aria-live", "polite")
  const notice = element(document, "p", "nexpoly-draft-notice")
  notice.setAttribute("role", "status")
  append(root, status, notice)
  const form = element(document, "form", "nexpoly-draft-form")
  form.noValidate = true
  form.addEventListener("submit", event => { event.preventDefault() })
  append(root, form)
  const reference = element(document, "details", "nexpoly-draft-reference")
  append(reference, element(document, "summary", "", "可选：用参考结构填写描述符"),
    element(document, "p", "nexpoly-note", "参考 SMILES 仅用于计算描述符。点击后会替换全部 15 项数值，不要求生成候选保留参考骨架。"))
  const referenceLabel = element(document, "label", "nexpoly-draft-reference-label", "参考结构 SMILES")
  const referenceInput = element(document, "input", "nexpoly-draft-input")
  referenceInput.type = "text"
  referenceInput.maxLength = 2048
  referenceInput.autocomplete = "off"
  referenceInput.spellcheck = false
  referenceInput.dataset.polytaoReference = ""
  referenceInput.value = draft.reference_smiles ?? ""
  referenceInput.setAttribute("aria-label", "参考结构 SMILES")
  append(referenceLabel, referenceInput)
  const referenceButton = element(document, "button", "nexpoly-draft-button", "计算并替换全部描述符")
  referenceButton.type = "button"
  referenceInput.addEventListener("input", () => refreshState())
  append(form, append(reference, referenceLabel, referenceButton))

  function buildField(name, label, help, schema, descriptor = false) {
    const box = element(document, "label", "nexpoly-draft-field")
    const title = element(document, "span", "nexpoly-draft-field-title", label + (descriptor ? " *" : ""))
    const input = element(document, "input", "nexpoly-draft-input")
    input.type = "text"
    input.inputMode = schema.type === "integer" ? "numeric" : "decimal"
    input.autocomplete = "off"
    input.spellcheck = false
    input.maxLength = 80
    input.setAttribute("aria-label", label)
    if (descriptor) input.dataset.descriptorName = name
    else input.dataset.parameterName = name
    const source = element(document, "span", "nexpoly-draft-source")
    const description = element(document, "span", "nexpoly-draft-help", help)
    const error = element(document, "span", "nexpoly-draft-field-error")
    error.dataset.fieldError = name
    error.setAttribute("aria-live", "polite")
    input.addEventListener("input", () => {
      dirty = true
      fields.get(name).touched = true
      noticeText = "参数尚未保存。确认时将先保存并校验本次输入。"
      const checked = polytaoNumericInput(input.value, schema)
      error.textContent = checked.error ?? ""
      if (checked.error) input.setAttribute("aria-invalid", "true")
      else input.removeAttribute("aria-invalid")
      if (descriptor) source.textContent = `${name} · ${input.value.trim() ? "用户填写（未保存）" : "待填写"}`
      refreshState()
    })
    fields.set(name, { input, source, error, schema, descriptor, touched: false })
    return append(box, title, input, source, description, error)
  }
  const fieldDefinitions = new Map(draft.descriptor_fields.map(field => [field.name, field]))
  const grouped = new Set()
  for (const group of DESCRIPTOR_GROUPS) {
    const definitions = group.names.filter(name => fieldDefinitions.has(name)).map(name => fieldDefinitions.get(name))
    if (!definitions.length) continue
    const section = element(document, "details", "nexpoly-draft-group")
    section.open = groups.length === 0
    const summary = element(document, "summary", "", group.label)
    append(section, summary)
    groups.push({ summary, names: definitions.map(field => field.name), label: group.label })
    const grid = element(document, "div", "nexpoly-draft-grid")
    for (const field of definitions) {
      grouped.add(field.name)
      append(grid, buildField(field.name, field.label + (field.unit ? `（${field.unit}）` : ""),
        field.description || DESCRIPTOR_HELP[field.name] || field.name, { type: "number" }, true))
    }
    append(form, append(section, grid))
  }
  const extra = draft.descriptor_fields.filter(field => !grouped.has(field.name))
  if (extra.length) {
    const section = element(document, "fieldset", "nexpoly-draft-fieldset")
    append(section, element(document, "legend", "", "其他描述符"))
    for (const field of extra) append(section, buildField(field.name, field.label, field.description, { type: "number" }, true))
    append(form, section)
  }
  const sampling = element(document, "fieldset", "nexpoly-draft-fieldset")
  append(sampling, element(document, "legend", "", "生成设置"),
    buildField("candidate_count", PARAMETER_LABELS.candidate_count, PARAMETER_HELP.candidate_count, draft.parameter_fields.candidate_count))
  const advanced = element(document, "details", "nexpoly-draft-advanced")
  append(advanced, element(document, "summary", "", "高级采样设置（已有默认值）"))
  const advancedGrid = element(document, "div", "nexpoly-draft-grid")
  for (const name of POLYTAO_PARAMETERS.slice(1)) append(advancedGrid,
    buildField(name, PARAMETER_LABELS[name], PARAMETER_HELP[name], draft.parameter_fields[name]))
  append(form, append(sampling, append(advanced, advancedGrid)))
  form.insertBefore(sampling, reference.nextSibling)
  const missing = element(document, "p", "nexpoly-note")
  missing.dataset.polytaoDraftMissing = ""
  append(form, missing)
  const buttons = element(document, "div", "nexpoly-draft-actions")
  const saveButton = element(document, "button", "nexpoly-draft-button", "保存参数")
  const confirmButton = element(document, "button", "nexpoly-draft-button nexpoly-draft-primary", "确认并生成")
  const cancelButton = element(document, "button", "nexpoly-draft-button nexpoly-draft-cancel", "取消草稿")
  const reloadButton = element(document, "button", "nexpoly-draft-button", "重新载入已保存参数")
  const continueButton = element(document, "button", "nexpoly-draft-button", "继续查询结果")
  const recoverButton = element(document, "button", "nexpoly-draft-button", "恢复已确认任务")
  for (const button of [saveButton, confirmButton, cancelButton, reloadButton, continueButton, recoverButton]) button.type = "button"
  append(form, append(buttons, saveButton, confirmButton, cancelButton, reloadButton, continueButton, recoverButton))
  const receipt = element(document, "p", "nexpoly-note nexpoly-draft-receipt")
  receipt.dataset.polytaoDraftReceipt = ""
  append(root, receipt)

  function readInputs(showErrors = false) {
    const descriptors = {}, parameters = {}, errors = [], missing = []
    for (const [name, field] of fields) {
      const result = polytaoNumericInput(field.input.value, field.schema)
      if (result.error) errors.push({ field: name, message: result.error })
      if (field.descriptor && result.value === null) missing.push(name)
      ;(field.descriptor ? descriptors : parameters)[name] = result.value
      if (showErrors) {
        field.error.textContent = result.error ?? ""
        if (result.error) field.input.setAttribute("aria-invalid", "true")
        else field.input.removeAttribute("aria-invalid")
      }
    }
    return { descriptors, parameters, errors, missing }
  }

  function refreshState() {
    const input = readInputs()
    const locked = busy || syncing || stale || !draft.editable || draft.status !== "editing"
    root.dataset.draftRevision = String(draft.revision)
    root.dataset.draftStatus = draft.status
    root.dataset.draftDirty = String(dirty)
    root.setAttribute("aria-busy", String(busy || syncing))
    status.textContent = `${DRAFT_STATUS_LABEL[draft.status]} · ${busy ? "正在处理，请稍候" : dirty ? "有未保存的修改" : `已保存第 ${draft.revision} 版`}`
    notice.textContent = noticeText
    notice.hidden = !noticeText
    for (const field of fields.values()) field.input.disabled = locked
    referenceInput.disabled = locked
    referenceButton.disabled = locked || !referenceInput.value.trim()
    saveButton.disabled = locked || input.errors.length > 0
    confirmButton.disabled = locked || input.errors.length > 0 || input.missing.length > 0
    cancelButton.disabled = locked
    reloadButton.disabled = busy || syncing
    continueButton.hidden = !draft.job_id || draft.status !== "submitted"
    continueButton.disabled = busy || syncing
    recoverButton.hidden = draft.status !== "confirmed" || Boolean(draft.job_id)
    recoverButton.disabled = busy || syncing
    receipt.hidden = !draft.job_id && !["submission_unknown", "rejected"].includes(draft.status)
    receipt.textContent = draft.job_id ? `任务已接受：${draft.job_id}。${continuation?.status === "failed"
      ? "自动查询暂未启动，可点击“继续查询结果”；不会重新提交生成。" : "将在当前对话继续查询并展示结果。"}`
      : draft.status === "submission_unknown" ? "提交结果尚不确定，已停止重复提交。请保留此草稿以便核查。"
        : draft.status === "rejected" ? "生成请求未被接受，此草稿已停止提交。" : ""
    missing.textContent = draft.editable && draft.status === "editing" ? input.errors.length
      ? `有 ${input.errors.length} 项数值需要修正。` : input.missing.length
        ? `还需填写 ${input.missing.length} / 15 项必填描述符。空白表示未知，不会按 0 提交。`
        : "15 项描述符已填写，可确认生成。生成结果不保证精确满足目标数值。" : ""
    for (const group of groups) {
      const remaining = group.names.filter(name => input.missing.includes(name)).length
      group.summary.textContent = `${group.label} · ${remaining ? `还需 ${remaining} 项` : "已填写"}`
    }
  }

  function adopt(next, force = false) {
    if (!validPolytaoDraft(next) || next.draft_id !== draft.draft_id || next.conversation_id !== draft.conversation_id) {
      throw new Error("INVALID_DRAFT_RESPONSE")
    }
    if (!force && next.revision < draft.revision) return
    if (!force && next.revision === draft.revision && (Date.parse(next.updated_at) < Date.parse(draft.updated_at)
        || !draft.editable && next.editable)) return
    if (!force && dirty && next.editable && next.revision > draft.revision) {
      stale = true
      noticeText = "草稿已在其他位置更新，请重新载入已保存参数后再确认。"
      refreshState()
      return
    }
    if (!force && dirty && next.editable) return
    draft = next
    dirty = false
    stale = false
    for (const [name, field] of fields) {
      const value = field.descriptor ? draft.descriptors[name] : draft.parameters[name]
      field.input.value = value == null ? "" : String(value)
      field.touched = false
      field.input.removeAttribute("aria-invalid")
      field.error.textContent = ""
      field.source.textContent = field.descriptor
        ? `${name} · ${own(draft.descriptors, name) ? draft.descriptor_sources[name] === "reference" ? "参考结构计算" : "用户填写" : "待填写"}`
        : `默认值：${draft.parameter_fields[name].default}；留空恢复默认值`
    }
    referenceInput.value = draft.reference_smiles ?? ""
    for (const error of draft.errors) {
      const target = fields.get(error.field.replace(/^descriptors\./, "").replace(/^parameters\./, ""))
      if (target) { target.error.textContent = error.message; target.input.setAttribute("aria-invalid", "true") }
    }
    refreshState()
  }

  async function perform(action, extra = {}) {
    const response = await actions.perform(action, { draft_id: draft.draft_id, revision: draft.revision, ...extra })
    if (!object(response) || response.ok !== true || !validPolytaoDraft(response.draft)) {
      const error = new Error("INVALID_DRAFT_RESPONSE")
      error.code = response?.error?.code
      throw error
    }
    continuation = response.continuation ?? null
    adopt(response.draft, true)
    return response.draft
  }

  async function run(operation) {
    if (busy || syncing) return
    busy = true
    noticeText = "正在处理，请稍候……"
    refreshState()
    try { await operation() }
    catch (error) {
      // A lost acknowledgement must never become an automatic confirm retry.
      syncing = false
      stale = true
      noticeText = BRIDGE_ERROR_LABEL[error?.code]
        ?? "操作尚未确认完成。请重新载入已保存参数，核对状态后继续；页面不会自动重复提交。"
    } finally { busy = false; refreshState() }
  }

  async function save() {
    const input = readInputs(true)
    if (input.errors.length) throw new Error("INVALID_INPUT")
    // Untouched reference values keep their provenance. Only explicit user
    // edits become descriptor patches, including null when a value is cleared.
    const descriptors = Object.fromEntries(Object.entries(input.descriptors).filter(([name]) => fields.get(name).touched))
    const next = await perform("update", { descriptors, parameters: input.parameters })
    noticeText = next.ready ? "参数已保存并通过校验，等待确认生成。" : "草稿已保存，补齐描述符后可确认生成。"
    return next
  }
  saveButton.addEventListener("click", () => run(save))
  confirmButton.addEventListener("click", () => run(async () => {
    const input = readInputs(true)
    if (input.errors.length || input.missing.length) return
    // Confirm the displayed saved revision directly. Only new edits require
    // another durable write and acknowledgement before the human confirmation.
    const next = dirty ? await save() : draft
    if (!next.ready || !next.editable || next.status !== "editing") return
    await perform("confirm")
    noticeText = draft.job_id ? "生成任务已提交，正在当前对话中继续查询。" : "确认状态已保存，请查看任务状态。"
    actions.onChange?.()
  }))
  referenceButton.addEventListener("click", () => run(async () => {
    const smiles = referenceInput.value.trim()
    if (!smiles) return
    await save()
    await perform("reference", { reference_smiles: smiles })
    noticeText = "已根据参考结构计算并替换全部 15 项描述符，请核对后确认生成。"
  }))
  cancelButton.addEventListener("click", () => run(async () => {
    await perform("cancel")
    noticeText = "草稿已取消，未创建生成任务。"
  }))
  continueButton.addEventListener("click", () => run(async () => {
    await perform("continue")
    noticeText = continuation?.status === "failed" ? "当前对话暂时无法继续查询，可稍后重试。生成任务不会重复提交。"
      : "已在当前对话继续查询现有任务。"
    actions.onChange?.()
  }))
  recoverButton.addEventListener("click", () => run(async () => {
    await perform("confirm")
    noticeText = draft.job_id ? "已恢复同一次确认的任务，正在当前对话继续查询。" : "确认状态已保存，请重新载入查看任务状态。"
    actions.onChange?.()
  }))
  async function reload() {
    if (busy) return
    syncing = true
    noticeText = "正在恢复最新草稿……"
    refreshState()
    try {
      await perform("get")
      noticeText = draft.status === "editing" ? "已载入最新保存参数；确认后才会启动生成任务。" : "已恢复最新草稿和任务状态。"
    } catch {
      stale = true
      noticeText = "暂时无法读取最新草稿，请点击重新载入后再操作。"
    } finally { syncing = false; refreshState() }
  }
  reloadButton.addEventListener("click", reload)
  root.nexpolySetDraft = next => { try { adopt(next) } catch { /* Reject uncorrelated saved output. */ } }
  adopt(draft, true)
  queueMicrotask(reload)
  return root
}

// Retrosynthesis projects only exact, completed capability-gateway tool outputs.
const RETRO_TOOLS = new Map(["prepare_monomer_retrosynthesis", "get_retrosynthesis_draft", "get_retrosynthesis_job"]
  .map(name => ["capability_gateway_" + name, name]))
const RETRO_DRAFT_ID = /^rtd_[a-f0-9]{32}$/
const RETRO_STATES = new Set(["pending", "queued", "running", "completed", "failed", "timed_out", "interrupted", "unknown"])
const RETRO_ROLES = ["auto", "diamine", "dianhydride", "other"]
const RETRO_DRAFT_STATES = new Set(["editing", "confirmed", "submitted", "submission_unknown", "rejected", "cancelled"])
const RETRO_STATUS = { pending: "等待派发", queued: "等待 GPU", running: "运行中", completed: "已完成",
  failed: "失败", timed_out: "排队超时", interrupted: "已中断", unknown: "执行结果待核验" }
const RETRO_BRIDGE_ERRORS = {
  INVALID_ARGUMENT: "目标结构或参数未通过校验。请检查 SMILES、来源和数值范围后再保存。",
  DRAFT_INCOMPLETE: "请先选择并保存一个明确的目标结构。",
  STRUCTURE_LIBRARY_UNAVAILABLE: "个人结构库当前未启用，请改用 SMILES 或本地名称匹配。",
  TARGET_SOURCE_UNAVAILABLE: "本地名称数据当前不可用，请改用 SMILES 或个人结构库。",
  SESSION_EXPIRED: "开发验证账号的登录已过期，请联系操作人员续期后重新载入原草稿。",
  MODEL_UNAVAILABLE: "逆合成模型或任务服务暂不可用，请稍后重新载入原草稿。",
  CAPACITY_LIMITED: "当前任务容量已满，请稍后重试原草稿。",
  PERMISSION_DENIED: "当前授权账号没有此操作权限，请联系操作人员。",
}
const RETRO_STAGE = { ...RETRO_STATUS, awaiting_verification: "等待核验", pending: "等待派发", queued: "等待 GPU", waiting_for_gpu: "等待 GPU",
  loading_model: "加载模型", predicting: "预测前体", processing_results: "处理结果", saving_results: "保存结果" }
const RETRO_PARAMETERS = { num_return_sequences: { label: "候选数量", minimum: 1, maximum: 10, default: 5 },
  num_beams: { label: "搜索宽度（Beam）", minimum: 1, maximum: 20, default: 5 },
  max_new_tokens: { label: "最大生成长度", minimum: 16, maximum: 256, default: 128 } }
const retroResolutionMessage = message => ({
  "Select a saved target structure.": "请选择一项个人结构。",
  "Choose the intended structure or supply SMILES.": "请从候选中选择目标，或提供 SMILES。",
  "No local exact name match; supply SMILES or select a saved structure.": "本地名称未命中，请提供 SMILES 或选择个人结构。",
})[message] ?? message
const roleLabel = role => ({ auto: "自动识别", diamine: "二胺", dianhydride: "二酐", other: "其他" })[role] ?? role
function validRetroParameters(value) {
  return object(value) && Object.keys(value).length === 4 && RETRO_ROLES.includes(value.target_role)
    && Object.entries(RETRO_PARAMETERS).every(([key, spec]) => Number.isSafeInteger(value[key])
      && value[key] >= spec.minimum && value[key] <= spec.maximum)
    && value.num_return_sequences <= value.num_beams
}
function validRetroResolved(value) {
  return object(value) && nonempty(value.canonical_smiles) && value.canonical_smiles.length <= 8000
    && nullableString(value.input_smiles) && RETRO_ROLES.includes(value.inferred_target_role)
    && nullableString(value.preview_svg) && (value.preview_svg?.length ?? 0) <= 256 * 1024
    && object(value.source) && ["smiles", "name", "saved_structure"].includes(value.source.type)
    && nullableString(value.source.label) && nullableString(value.source.record_id)
}
export function validRetrosynthesisDraft(value) {
  return object(value) && value.ok === true && RETRO_DRAFT_ID.test(value.draft_id ?? "")
    && /^ses_[A-Za-z0-9_-]+$/.test(value.conversation_id ?? "") && positive(value.revision)
    && RETRO_DRAFT_STATES.has(value.status) && typeof value.ready === "boolean" && typeof value.editable === "boolean"
    && (value.target == null || object(value.target) && ["smiles", "name", "saved_structure"].includes(value.target.source))
    && (value.resolved_target == null || validRetroResolved(value.resolved_target))
    && Array.isArray(value.target_candidates) && value.target_candidates.length <= 20
    && value.target_candidates.every(validRetroResolved) && nullableString(value.next_cursor)
    && validRetroParameters(value.parameters) && object(value.parameter_fields)
    && strings(value.missing_fields) && Array.isArray(value.errors)
    && value.errors.every(error => object(error) && nonempty(error.field) && string(error.message))
    && timestamp(value.created_at) && timestamp(value.updated_at) && nullableString(value.job_id)
    && nullableString(value.submission_error) && (!value.ready || value.resolved_target != null && !value.errors.length)
}
function validRetroCandidate(value) {
  return object(value) && positive(value.rank) && string(value.reactants_smiles)
    && nullableString(value.canonical_reactants_smiles) && typeof value.valid_smiles === "boolean"
    && (value.all_reactants_smaller_than_target == null || typeof value.all_reactants_smaller_than_target === "boolean") && nullableString(value.reaction_hint)
    && Array.isArray(value.reactants) && value.reactants.every(item => object(item)
      && string(item.input_smiles) && nullableString(item.canonical_smiles) && typeof item.valid_smiles === "boolean"
      && (item.heavy_atom_count == null || count(item.heavy_atom_count)))
}
function validRetroJob(value, input) {
  if (!object(value) || value.ok !== true || !nonempty(value.job_id) || input.job_id !== value.job_id
      || !RETRO_STATES.has(value.status) || !timestamp(value.snapshot_at) || !timestamp(value.created_at)
      || !timestamp(value.updated_at) || !optionalTimestamp(value.started_at) || !optionalTimestamp(value.finished_at)
      || !nullableString(value.stage) || !nullableString(value.error_code) || !strings(value.warnings)) return false
  if (value.result == null) return value.status !== "completed"
  return value.status === "completed" && object(value.result) && nonempty(value.result.canonical_smiles)
    && Array.isArray(value.result.candidates) && value.result.candidates.length <= 10
    && value.result.total === value.result.candidates.length && value.result.candidates.every(validRetroCandidate)
    && new Set(value.result.candidates.map(candidate => candidate.rank)).size === value.result.candidates.length
}
export function extractRetrosynthesisDrafts(messages) {
  if (!Array.isArray(messages)) return []
  const users = new Set(messages.filter(message => message?.info?.role === "user" && nonempty(message.info.id)).map(message => message.info.id))
  const drafts = new Map()
  for (const message of messages) {
    if (message?.info?.role !== "assistant" || !users.has(message.info.parentID) || !Array.isArray(message.parts)) continue
    for (const part of message.parts) {
      const name = RETRO_TOOLS.get(part?.tool)
      if (part?.type !== "tool" || !["prepare_monomer_retrosynthesis", "get_retrosynthesis_draft"].includes(name)
          || part.state?.status !== "completed" || !object(part.state.input)) continue
      const draft = successfulOutput(part)
      if (!validRetrosynthesisDraft(draft) || part.state.input.draft_id != null && part.state.input.draft_id !== draft.draft_id
          || part.state.input.conversation_id != null && part.state.input.conversation_id !== draft.conversation_id
          || message.info.sessionID != null && message.info.sessionID !== draft.conversation_id) continue
      const previous = drafts.get(draft.draft_id)
      if (previous && (previous.draft.conversation_id !== draft.conversation_id || previous.draft.revision > draft.revision
          || previous.draft.revision === draft.revision && Date.parse(previous.draft.updated_at) > Date.parse(draft.updated_at))) continue
      drafts.set(draft.draft_id, { kind: "retrosynthesis-draft", key: `retrosynthesis-draft:${draft.draft_id}`,
        turnId: previous?.turnId ?? message.info.parentID, draft })
    }
  }
  return [...drafts.values()]
}
export function extractRetrosynthesisTurns(messages) {
  if (!Array.isArray(messages)) return []
  const users = new Map(messages.filter(message => message?.info?.role === "user" && nonempty(message.info.id))
    .map(message => [message.info.id, new Map()]))
  for (const message of messages) {
    if (message?.info?.role !== "assistant" || !users.has(message.info.parentID) || !Array.isArray(message.parts)) continue
    for (const part of message.parts) {
      if (part?.type !== "tool" || RETRO_TOOLS.get(part.tool) !== "get_retrosynthesis_job"
          || part.state?.status !== "completed" || !object(part.state.input)) continue
      const snapshot = successfulOutput(part)
      if (!validRetroJob(snapshot, part.state.input)) continue
      const jobs = users.get(message.info.parentID), previous = jobs.get(snapshot.job_id)
      if (previous && Date.parse(previous.snapshot.snapshot_at) > Date.parse(snapshot.snapshot_at)) continue
      jobs.set(snapshot.job_id, { kind: "retrosynthesis", key: `retrosynthesis:${message.info.parentID}:${snapshot.job_id}`,
        turnId: message.info.parentID, jobId: snapshot.job_id, snapshot })
    }
  }
  return [...users.values()].flatMap(jobs => [...jobs.values()])
}
function retroPreview(document, target) {
  const box = element(document, "div", "nexpoly-retro-preview")
  if (target?.preview_svg) {
    // An image document does not execute SVG scripts; never put source markup into the page DOM.
    const image = element(document, "img", "nexpoly-retro-structure")
    image.alt = "目标分子二维结构"
    image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(target.preview_svg)
    append(box, image)
  }
  if (target) append(box, element(document, "code", "nexpoly-smiles", target.canonical_smiles),
    element(document, "p", "nexpoly-note", `检测角色：${roleLabel(target.inferred_target_role)}${target.source.label ? ` · 来源：${target.source.label}` : ""}`))
  return box
}
export function renderRetrosynthesisResult(document, model) {
  if (!document?.createElement || model?.kind !== "retrosynthesis" || !validRetroJob(model.snapshot, { job_id: model.jobId })) {
    throw new TypeError("An extracted retrosynthesis job is required")
  }
  const value = model.snapshot, result = value.result
  const root = element(document, "section", "nexpoly-results nexpoly-retrosynthesis")
  root.dataset.nexpolyResults = ""; root.dataset.retrosynthesisResults = ""; root.dataset.turnId = model.turnId
  root.dataset.jobId = value.job_id
  root.setAttribute("aria-label", "单体逆合成结果")
  append(root, append(element(document, "header", "nexpoly-results-header"),
    element(document, "p", "nexpoly-eyebrow", "NEXPOLY · 单体逆合成"), element(document, "h2", "", "单步前体预测结果")))
  const summary = element(document, "dl", "nexpoly-polytao-summary")
  for (const [label, text] of [["任务状态", RETRO_STATUS[value.status]], ["执行阶段", RETRO_STAGE[value.stage] ?? value.stage ?? "—"],
    ["查询时间", new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "long" }).format(new Date(value.snapshot_at))]]) {
    const metric = element(document, "div", "nexpoly-polytao-metric"); field(document, metric, label, text); append(summary, metric)
  }
  append(root, summary, element(document, "p", "nexpoly-semantics", "以下为单步前体预测候选。排名不是置信度；结构有效不代表反应可实现，也不包含完整实验路线或反应条件。"))
  if (value.status === "unknown") append(root, element(document, "p", "nexpoly-result-issue", "任务曾开始执行，完成结果仍待核验。请继续查询原任务，不要重复提交。"))
  else if (["pending", "queued", "running"].includes(value.status)) append(root, element(document, "p", "nexpoly-note", "这是最近一次真实查询的快照，可继续查询该任务。"))
  if (value.error_code) append(root, element(document, "p", "nexpoly-result-issue", `任务状态说明：${value.error_code}`))
  for (const warning of value.warnings) append(root, element(document, "p", "nexpoly-result-issue", warning))
  if (result) {
    append(root, element(document, "p", "nexpoly-note", `目标：${result.canonical_smiles}`),
      element(document, "p", "nexpoly-note", `用途标签：${roleLabel(result.target_role ?? "auto")} · 检测角色：${roleLabel(result.inferred_target_role ?? "other")}`))
    if (!result.candidates.length) append(root, element(document, "p", "nexpoly-empty", "任务已完成，本次没有返回前体候选。"))
    for (const candidate of result.candidates) {
      const card = element(document, "article", "nexpoly-material nexpoly-retro-candidate")
      card.dataset.retrosynthesisCandidate = String(candidate.rank)
      append(card, element(document, "h3", "nexpoly-section-title", `候选 ${candidate.rank}`),
        element(document, "p", candidate.valid_smiles ? "nexpoly-note" : "nexpoly-result-issue",
          `结构解析：${candidate.valid_smiles ? "有效" : "无效"} · 前体均小于目标：${candidate.all_reactants_smaller_than_target == null ? "未判断" : candidate.all_reactants_smaller_than_target ? "是" : "否"}`))
      const smiles = candidate.canonical_reactants_smiles || candidate.reactants_smiles
      append(card, element(document, "code", "nexpoly-smiles", smiles))
      const copy = element(document, "button", "nexpoly-copy-smiles", "复制前体 SMILES"), feedback = element(document, "span", "nexpoly-copy-status")
      copy.type = "button"; feedback.setAttribute("role", "status")
      copy.addEventListener("click", async () => {
        try { await document.defaultView.navigator.clipboard.writeText(smiles); feedback.textContent = "已复制" }
        catch { feedback.textContent = "复制失败，请手动复制。" }
      })
      append(card, copy, feedback)
      for (const [index, item] of candidate.reactants.entries()) append(card, element(document, "p", "nexpoly-note",
        `前体 ${index + 1}：${item.canonical_smiles || item.input_smiles}（${item.valid_smiles ? "有效" : "无效"}）`))
      if (candidate.reaction_hint) append(card, element(document, "p", "nexpoly-note", candidate.reaction_hint))
      append(root, card)
    }
  }
  const details = element(document, "details", "nexpoly-provenance"), task = element(document, "dl", "nexpoly-provenance-data")
  append(details, element(document, "summary", "", "任务详情")); field(document, task, "任务编号", value.job_id)
  return append(root, append(details, task))
}

export function renderRetrosynthesisDraft(document, model, actions) {
  if (!document?.createElement || model?.kind !== "retrosynthesis-draft" || !validRetrosynthesisDraft(model.draft)
      || typeof actions?.perform !== "function") throw new TypeError("An extracted retrosynthesis draft and bridge are required")
  let draft = model.draft, busy = false, syncing = true, stale = false, dirty = false, beamsTouched = false, continuation = null
  let noticeText = "正在恢复最新草稿……", candidates = [], nextCursor = null, targetDirty = false
  const touchedParameters = new Set()
  const root = element(document, "section", "nexpoly-results nexpoly-retro-form")
  root.dataset.nexpolyResults = ""; root.dataset.retrosynthesisDraft = draft.draft_id; root.dataset.turnId = model.turnId
  root.setAttribute("aria-label", "单体逆合成参数")
  append(root, append(element(document, "header", "nexpoly-results-header"), element(document, "p", "nexpoly-eyebrow", "NEXPOLY · 单体逆合成"),
    element(document, "h2", "", "确认目标单体与预测参数")), element(document, "p", "nexpoly-note", "可输入 SMILES、匹配本地名称或选择授权账号的个人结构。解析和保存均不会启动推理；确认后提交一次。"))
  const status = element(document, "p", "nexpoly-draft-status"), notice = element(document, "p", "nexpoly-draft-notice")
  status.setAttribute("role", "status"); notice.setAttribute("role", "status"); append(root, status, notice)
  const form = element(document, "form", "nexpoly-draft-form")
  form.addEventListener("submit", event => event.preventDefault()); append(root, form)
  const sourceLabel = element(document, "label", "nexpoly-draft-field", "目标来源"), source = element(document, "select", "nexpoly-draft-input")
  source.setAttribute("aria-label", "目标来源")
  for (const [value, text] of [["smiles", "SMILES"], ["name", "本地名称"], ["saved_structure", "个人结构库"]]) {
    const option = element(document, "option", "", text); option.value = value; append(source, option)
  }
  append(form, append(sourceLabel, source))
  const targetLabel = element(document, "label", "nexpoly-draft-field", "目标结构或名称"), target = element(document, "textarea", "nexpoly-draft-input")
  target.setAttribute("aria-label", "目标结构或名称"); target.rows = 3; target.maxLength = 8000; target.spellcheck = false
  const targetHelp = element(document, "p", "nexpoly-draft-help"), preview = element(document, "div", "nexpoly-retro-preview-host")
  const resolveButton = element(document, "button", "nexpoly-draft-button", "解析 / 搜索结构"), moreButton = element(document, "button", "nexpoly-draft-button", "更多结构")
  resolveButton.type = moreButton.type = "button"
  const choices = element(document, "div", "nexpoly-retro-choices"); choices.setAttribute("aria-label", "目标候选")
  append(form, append(targetLabel, target), targetHelp, resolveButton, choices, moreButton, preview)
  const roleBox = element(document, "label", "nexpoly-draft-field", "用途标签"), role = element(document, "select", "nexpoly-draft-input")
  role.setAttribute("aria-label", "用途标签")
  for (const value of RETRO_ROLES) { const option = element(document, "option", "", roleLabel(value)); option.value = value; append(role, option) }
  append(form, append(roleBox, role), element(document, "p", "nexpoly-draft-help", "用途标签不约束模型生成；检测角色来自目标结构。"))
  const fields = new Map(), basic = element(document, "div", "nexpoly-draft-grid"), advanced = element(document, "details", "nexpoly-draft-advanced")
  append(advanced, element(document, "summary", "", "高级预测参数（已有默认值）"))
  for (const [name, spec] of Object.entries(RETRO_PARAMETERS)) {
    const label = element(document, "label", "nexpoly-draft-field", spec.label), input = element(document, "input", "nexpoly-draft-input")
    input.type = "text"; input.inputMode = "numeric"; input.maxLength = 10; input.dataset.parameterName = name; input.setAttribute("aria-label", spec.label)
    fields.set(name, input); append(name === "num_return_sequences" ? basic : advanced, append(label, input))
    input.addEventListener("input", () => {
      touchedParameters.add(name)
      if (name === "num_beams") beamsTouched = Boolean(input.value.trim())
      if (name === "num_return_sequences" && !beamsTouched) {
        const checked = polytaoNumericInput(input.value, { type: "integer", ...spec })
        if (!checked.error && checked.value != null) fields.get("num_beams").value = String(Math.max(5, checked.value))
      }
      changed()
    })
  }
  append(form, basic, advanced, element(document, "p", "nexpoly-draft-help", "生成长度以模型词元计。候选数量是上限，实际返回可能较少。"))
  const buttons = element(document, "div", "nexpoly-draft-actions"), saveButton = element(document, "button", "nexpoly-draft-button", "保存参数"),
    confirmButton = element(document, "button", "nexpoly-draft-button nexpoly-draft-primary", "确认并预测"),
    cancelButton = element(document, "button", "nexpoly-draft-button", "取消草稿"),
    reloadButton = element(document, "button", "nexpoly-draft-button", "重新载入已保存参数"),
    continueButton = element(document, "button", "nexpoly-draft-button", "继续查询结果"),
    recoverButton = element(document, "button", "nexpoly-draft-button", "重试原提交")
  for (const button of [saveButton, confirmButton, cancelButton, reloadButton, continueButton, recoverButton]) { button.type = "button"; append(buttons, button) }
  const errors = element(document, "p", "nexpoly-result-issue"), receipt = element(document, "p", "nexpoly-note")
  append(form, errors, buttons); append(root, receipt)
  function targetInput(cursor) {
    const text = target.value.trim()
    if (source.value === "smiles") return text ? { source: "smiles", smiles: text } : null
    if (source.value === "name") return text ? { source: "name", name: text } : null
    if (target.dataset.recordId && !targetDirty) return { source: "saved_structure", record_id: target.dataset.recordId }
    return { source: "saved_structure", query: text, limit: 20, ...(cursor ? { cursor } : {}) }
  }
  function parameters() {
    const result = { target_role: role.value }, problems = []
    for (const [name, input] of fields) {
      const checked = polytaoNumericInput(input.value, { type: "integer", ...RETRO_PARAMETERS[name] })
      result[name] = checked.value ?? RETRO_PARAMETERS[name].default
      if (checked.error) problems.push(`${RETRO_PARAMETERS[name].label}：${checked.error}`)
    }
    if (!fields.get("num_beams").value.trim() && !beamsTouched) result.num_beams = Math.max(5, result.num_return_sequences)
    if (result.num_return_sequences > result.num_beams) problems.push("搜索宽度不能小于候选数量。")
    return { values: result, problems }
  }
  function changed(targetChanged = false) {
    dirty = true
    if (targetChanged) { targetDirty = true; candidates = []; nextCursor = null; choices.replaceChildren(); target.dataset.recordId = "" }
    noticeText = "参数尚未保存，确认时会先校验并保存。"; refresh()
  }
  target.addEventListener("input", () => changed(true)); source.addEventListener("change", () => changed(true)); role.addEventListener("change", () => { touchedParameters.add("target_role"); dirty = true; refresh() })
  function refresh() {
    const checked = parameters(), locked = busy || syncing || stale || !draft.editable || draft.status !== "editing"
    root.dataset.draftRevision = String(draft.revision); root.dataset.draftStatus = draft.status; root.dataset.draftDirty = String(dirty)
    root.setAttribute("aria-busy", String(busy || syncing))
    status.textContent = `${DRAFT_STATUS_LABEL[draft.status] ?? draft.status} · ${dirty ? "有未保存的修改" : `已保存第 ${draft.revision} 版`}`
    notice.textContent = noticeText; notice.hidden = !noticeText
    for (const input of [source, target, role, ...fields.values()]) input.disabled = locked
    targetHelp.textContent = source.value === "saved_structure" ? "搜索当前授权原生账号的个人结构；选择一项后保存。" : source.value === "name"
      ? "仅匹配本地名称数据。多个结构需要选择；未命中时请提供 SMILES。" : "输入一个完整、确定的目标分子 SMILES。"
    resolveButton.disabled = locked || !targetInput(); saveButton.disabled = locked || checked.problems.length > 0
    confirmButton.disabled = locked || checked.problems.length > 0 || (!dirty && !draft.ready) || !targetInput()
    cancelButton.disabled = locked; reloadButton.disabled = busy || syncing
    moreButton.hidden = !nextCursor; moreButton.disabled = locked
    continueButton.hidden = !draft.job_id || draft.status !== "submitted"; continueButton.disabled = busy || syncing
    recoverButton.hidden = !["confirmed", "submission_unknown"].includes(draft.status) || Boolean(draft.job_id); recoverButton.disabled = busy || syncing
    errors.textContent = checked.problems.join(" ") || draft.errors.map(error => retroResolutionMessage(error.message)).join(" "); errors.hidden = !errors.textContent
    receipt.textContent = draft.job_id ? `任务已接受：${draft.job_id}。${continuation?.status === "failed" ? "可点击继续查询结果。" : "将在原对话中查询结果。"}`
      : draft.status === "submission_unknown" ? "提交回执待核查。点击“重试原提交”会先查询任务；若仍未查到，将使用已冻结的相同参数和幂等键再次发送。不会自动重试或生成新键。" : ""
    if (draft.status === "confirmed" && !draft.job_id) receipt.textContent = "确认参数已冻结。点击“重试原提交”会先查询任务，必要时以相同参数和幂等键发送；页面不会自动重试。"
    receipt.hidden = !receipt.textContent
  }
  function adopt(next, force = false) {
    if (!validRetrosynthesisDraft(next) || next.draft_id !== draft.draft_id || next.conversation_id !== draft.conversation_id) throw new Error("INVALID_DRAFT_RESPONSE")
    if (!force && (next.revision < draft.revision || next.revision === draft.revision && Date.parse(next.updated_at) < Date.parse(draft.updated_at))) return
    if (!force && dirty && next.editable) { if (next.revision > draft.revision) { stale = true; noticeText = "草稿已在其他位置更新，请重新载入。"; refresh() } return }
    draft = next; dirty = false; targetDirty = false; stale = false
    beamsTouched = (draft.explicit_parameters ?? []).includes("num_beams"); touchedParameters.clear()
    source.value = draft.target?.source ?? "smiles"
    target.value = draft.target?.smiles ?? draft.target?.name ?? draft.target?.query ?? draft.resolved_target?.source.label ?? draft.resolved_target?.canonical_smiles ?? ""
    target.dataset.recordId = draft.target?.record_id ?? ""
    role.value = draft.parameters.target_role
    for (const [name, input] of fields) input.value = String(draft.parameters[name])
    candidates = draft.target_candidates; nextCursor = draft.next_cursor
    preview.replaceChildren(retroPreview(document, draft.resolved_target)); showCandidates(); refresh()
  }
  function showCandidates() {
    choices.replaceChildren()
    for (const candidate of candidates) {
      const card = element(document, "div", "nexpoly-retro-choice"), choose = element(document, "button", "nexpoly-draft-button", `选择 ${candidate.source.label || candidate.canonical_smiles}`)
      choose.type = "button"; choose.disabled = busy || syncing || !draft.editable
      choose.addEventListener("click", () => run(async () => {
        const chosen = candidate.source.type === "saved_structure" && candidate.source.record_id
          ? { source: "saved_structure", record_id: candidate.source.record_id } : { source: "smiles", smiles: candidate.canonical_smiles }
        await perform("update", { target: chosen, parameters: parameterPatch() }); noticeText = "已选择并保存目标结构，请核对后确认。"
      }))
      append(choices, append(card, retroPreview(document, candidate), choose))
    }
  }
  async function perform(action, extra = {}) {
    const response = await actions.perform(action, { draft_id: draft.draft_id, revision: draft.revision, ...extra })
    if (response?.ok !== true) { const error = new Error("DRAFT_BRIDGE_FAILED"); error.code = response?.error?.code; throw error }
    if (action === "resolve") {
      const resolution = response.resolution
      if (!object(resolution) || resolution.ok !== true || !Array.isArray(resolution.candidates)
          || !resolution.candidates.every(validRetroResolved) || resolution.target != null && !validRetroResolved(resolution.target)) throw new Error("INVALID_RESOLUTION")
      candidates = resolution.candidates; nextCursor = resolution.next_cursor ?? null
      preview.replaceChildren(retroPreview(document, resolution.target)); showCandidates()
      noticeText = retroResolutionMessage(resolution.message) || (resolution.target ? "目标已解析，请保存或确认预测。" : "请选择一个结构，或提供更明确的 SMILES。")
      return resolution
    }
    if (!validRetrosynthesisDraft(response.draft)) throw new Error("INVALID_DRAFT_RESPONSE")
    continuation = response.continuation ?? null; adopt(response.draft, true); return response.draft
  }
  async function run(operation) {
    if (busy || syncing) return
    busy = true; refresh()
    try { await operation() }
    catch (error) {
      stale = !["INVALID_ARGUMENT", "DRAFT_INCOMPLETE", "STRUCTURE_LIBRARY_UNAVAILABLE", "TARGET_SOURCE_UNAVAILABLE", "CAPACITY_LIMITED"].includes(error?.code)
      noticeText = RETRO_BRIDGE_ERRORS[error?.code] ?? BRIDGE_ERROR_LABEL[error?.code]
        ?? "操作回执尚未确认。请重新载入并核对状态；页面不会自动重复提交。"
    }
    finally { busy = false; showCandidates(); refresh() }
  }
  function parameterPatch() {
    const values = parameters().values
    return Object.fromEntries([...touchedParameters].map(name => [name, name !== "target_role" && !fields.get(name).value.trim() ? null : values[name]]))
  }
  async function save() {
    const checked = parameters(); if (checked.problems.length) return null
    const next = await perform("update", { target: targetInput(), parameters: parameterPatch() })
    noticeText = next.ready ? "目标和参数已保存，等待确认预测。" : "草稿已保存，请先选择或补充明确目标。"; return next
  }
  saveButton.addEventListener("click", () => run(save))
  resolveButton.addEventListener("click", () => run(() => perform("resolve", { target: targetInput() })))
  moreButton.addEventListener("click", () => run(() => perform("resolve", { target: targetInput(nextCursor) })))
  confirmButton.addEventListener("click", () => run(async () => {
    const next = dirty ? await save() : draft
    if (!next?.ready || !next.editable || next.status !== "editing") return
    await perform("confirm"); noticeText = "任务已提交，正在原对话中查询。"; actions.onChange?.()
  }))
  cancelButton.addEventListener("click", () => run(async () => { await perform("cancel"); noticeText = "草稿已取消，未创建任务。" }))
  continueButton.addEventListener("click", () => run(async () => { await perform("continue"); noticeText = "已请求查询原任务。"; actions.onChange?.() }))
  recoverButton.addEventListener("click", () => run(async () => { await perform("confirm"); noticeText = "已重试原提交，参数和幂等键保持一致。"; actions.onChange?.() }))
  async function reload() {
    if (busy) return
    syncing = true; refresh()
    try { await perform("get"); noticeText = draft.editable ? "已恢复保存参数，确认后才会启动预测。" : "已恢复草稿与任务回执。" }
    catch { stale = true; noticeText = "暂时无法读取最新草稿，请重新载入。" }
    finally { syncing = false; showCandidates(); refresh() }
  }
  reloadButton.addEventListener("click", reload)
  root.nexpolySetDraft = next => { try { adopt(next) } catch { /* Reject unrelated saved output. */ } }
  adopt(draft, true); queueMicrotask(reload); return root
}
