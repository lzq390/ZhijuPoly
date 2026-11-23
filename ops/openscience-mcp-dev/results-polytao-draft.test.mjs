import assert from "node:assert/strict"
import { createServer } from "node:http"
import { existsSync, readFileSync } from "node:fs"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import path from "node:path"
import test from "node:test"
import { extractPolytaoDrafts, polytaoNumericInput, validPolytaoDraft } from "./results-view.mjs"
import { polytaoDraftBridgeBody } from "./results-controller.mjs"

const DIR = path.dirname(fileURLToPath(import.meta.url))
const NAMES = ["MolWt", "HeavyAtomCount", "NHOHCount", "NOCount", "NumAliphaticCarbocycles", "NumAliphaticHeterocycles",
  "NumAliphaticRings", "NumAromaticCarbocycles", "NumAromaticHeterocycles", "NumAromaticRings", "NumHAcceptors",
  "NumHDonors", "NumHeteroatoms", "NumRotatableBonds", "RingCount"]
const LABELS = ["分子量", "重原子数", "NH / OH 基团数", "氮氧原子数", "脂肪碳环数", "脂肪杂环数", "脂肪环总数",
  "芳香碳环数", "芳香杂环数", "芳香环总数", "氢键受体数", "氢键供体数", "杂原子数", "可旋转键数", "环总数"]
const PARAMETERS = { candidate_count: 3, temperature: 1, top_k: 100, top_p: .999, max_length: 300 }
const DRAFT_ID = "ptd_0123456789abcdef0123456789abcdef"
function draft(extra = {}) {
  const descriptors = extra.descriptors ?? { MolWt: 28.05 }
  const descriptor_sources = Object.fromEntries(Object.keys(descriptors).map(key => [key, "user"]))
  return { ok: true, request_id: "request-a", draft_id: DRAFT_ID, conversation_id: "ses_current", revision: 1,
    status: "editing", ready: Object.keys(descriptors).length === 15, editable: true,
    descriptors, descriptor_sources, parameters: { ...PARAMETERS },
    missing_fields: NAMES.filter(name => !Object.hasOwn(descriptors, name)), errors: [],
    descriptor_fields: NAMES.map((name, index) => ({ name, label: LABELS[index], description: "Field " + name,
      unit: index === 0 ? "g/mol" : null, required: true })),
    parameter_fields: { candidate_count: { type: "integer", default: 10, minimum: 1, maximum: 50 },
      temperature: { type: "number", default: 1, minimum: .1, maximum: 2 },
      top_k: { type: "integer", default: 100, minimum: 1, maximum: 500 },
      top_p: { type: "number", default: .999, exclusiveMinimum: 0, maximum: 1 },
      max_length: { type: "integer", default: 300, minimum: 16, maximum: 512 } },
    reference_smiles: null, canonical_smiles: null, job_id: null, submission_error: null, submission: null,
    created_at: "2026-11-16T01:00:00Z", updated_at: "2026-11-16T01:00:00Z", ...extra,
    descriptors, descriptor_sources: extra.descriptor_sources ?? descriptor_sources }
}
function tool(output, name = "prepare_polytao_generation", input = {}) {
  return { type: "tool", tool: "capability_gateway_" + name, callID: "call-" + output.revision,
    state: { status: "completed", input, output: "Authoritative draft\n" + JSON.stringify(output) } }
}
function messages(parts, user = "user-one") {
  return [{ info: { role: "user", id: user }, parts: [] },
    { info: { role: "assistant", id: "assistant-" + user, parentID: user }, parts }]
}
const complete = () => Object.fromEntries(NAMES.map((name, index) => [name, index === 0 ? 28.05 : 0]))

test("draft panels require exact completed MCP outputs and the complete authoritative field schema", () => {
  const output = draft()
  assert.equal(validPolytaoDraft(output), true)
  const genuine = tool(output)
  assert.equal(extractPolytaoDrafts(messages([genuine]))[0].draft.draft_id, DRAFT_ID)
  assert.deepEqual(extractPolytaoDrafts(messages([{ type: "text", text: JSON.stringify(output) }])), [])
  assert.deepEqual(extractPolytaoDrafts(messages([{ ...genuine, tool: "untrusted_prepare_polytao_generation" }])), [])
  assert.deepEqual(extractPolytaoDrafts(messages([{ ...genuine, state: { ...genuine.state, status: "running" } }])), [])
  assert.deepEqual(extractPolytaoDrafts(messages([tool(output, "get_polytao_draft", { draft_id: "ptd_other_12345" })])), [])
  for (const mutate of [
    value => { value.descriptors.MolWt = "28" }, value => { value.revision = 0 },
    value => { value.descriptor_fields.pop() }, value => { value.descriptor_fields[1].name = "MolWt" },
    value => { value.missing_fields.push(value.missing_fields[0]) }, value => { value.descriptor_sources.MolWt = "inferred" },
    value => { value.parameter_fields.top_k = {} }, value => { value.conversation_id = "../session" },
  ]) {
    const value = structuredClone(output); mutate(value)
    assert.equal(validPolytaoDraft(value), false)
    assert.deepEqual(extractPolytaoDrafts(messages([tool(value)])), [])
  }
})

test("one draft persists across turns without accepting older revisions or mutating source messages", () => {
  const initial = draft()
  const latest = draft({ revision: 3, descriptors: complete(), updated_at: "2026-11-16T01:03:00Z" })
  const older = draft({ revision: 2, updated_at: "2026-11-16T01:02:00Z" })
  const session = [...messages([tool(initial)]), ...messages([tool(latest, "get_polytao_draft", { draft_id: DRAFT_ID })], "user-two"),
    ...messages([tool(older)], "user-three")]
  const before = JSON.stringify(session)
  const models = extractPolytaoDrafts(session)
  assert.equal(models.length, 1)
  assert.equal(models[0].turnId, "user-one")
  assert.equal(models[0].draft.revision, 3)
  assert.equal(models[0].draft.ready, true)
  assert.equal(JSON.stringify(session), before)
  assert.deepEqual(extractPolytaoDrafts(JSON.parse(before)), models)
})

test("numeric form input keeps missing distinct from zero and checks finite values and sampling bounds", () => {
  for (const source of ["", "  "]) assert.deepEqual(polytaoNumericInput(source), { value: null, error: null })
  assert.deepEqual(polytaoNumericInput("0"), { value: 0, error: null })
  assert.deepEqual(polytaoNumericInput("1e-3"), { value: .001, error: null })
  for (const source of ["NaN", "Infinity", "1e400", "0x20", "true", "--3", "2 units"]) assert.ok(polytaoNumericInput(source).error)
  assert.ok(polytaoNumericInput("1.2", { type: "integer" }).error)
  assert.ok(polytaoNumericInput("0", { minimum: 1 }).error)
  assert.ok(polytaoNumericInput("3", { maximum: 2 }).error)
  assert.ok(polytaoNumericInput("0", { exclusiveMinimum: 0 }).error)
})

test("bridge requests bind the observed session, directory and revision and drop client/owner overrides", () => {
  const input = { draft_id: DRAFT_ID, revision: 2, descriptors: { MolWt: null }, session_id: "ses_attacker",
    client_id: "root", account_id: "someone", action: "confirm" }
  assert.deepEqual(polytaoDraftBridgeBody("ses_current", "/openscience/files/general", "update", input), {
    session_id: "ses_current", directory: "/openscience/files/general", action: "update", draft_id: DRAFT_ID,
    revision: 2, descriptors: { MolWt: null },
  })
  for (const [session, directory, action, data] of [
    ["ses_current", "/run/private", "get", input], ["ses_current", "/openscience/files/general", "submit", input],
    ["bad", "/openscience/files/general", "get", input],
    ["ses_current", "/openscience/files/general", "confirm", { ...input, revision: 0 }],
  ]) assert.throws(() => polytaoDraftBridgeBody(session, directory, action, data))
})

const playwrightPaths = [path.join(DIR, "..", "openscience-ui-overlay", "node_modules", "playwright"),
  "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright"]
const playwrightPath = playwrightPaths.find(value => existsSync(path.join(value, "package.json")))
const browserOptions = { skip: !playwrightPath && "The existing Playwright package is not installed" }

async function browserHarness(run) {
  const { chromium } = createRequire(import.meta.url)(playwrightPath)
  const server = createServer((request, response) => {
    const asset = new URL(request.url, "http://localhost").pathname
    if (["/results-view.mjs", "/results-controller.mjs", "/results-view.css"].includes(asset)) {
      response.setHeader("content-type", asset.endsWith("mjs") ? "text/javascript" : "text/css")
      response.end(readFileSync(path.join(DIR, asset.slice(1))))
    } else {
      response.setHeader("content-type", "text/html")
      response.end('<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/results-view.css"><main style="padding:12px"></main></html>')
    }
  })
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
  let browser
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] })
    const page = await browser.newPage({ viewport: { width: 452, height: 1000 } })
    await page.goto(`http://127.0.0.1:${server.address().port}`)
    await run(page)
  } finally { await browser?.close(); await new Promise(resolve => server.close(resolve)) }
}

async function mountForm(page, initial = draft()) {
  await page.evaluate(async ({ initial, names, all }) => {
    const { renderPolytaoDraft } = await import("/results-view.mjs")
    window.savedDraft = structuredClone(initial)
    window.operations = []
    window.onChangeCount = 0
    window.failConfirm = false
    window.formPanel = renderPolytaoDraft(document, { kind: "polytao-draft", turnId: "user-one", draft: initial }, {
      perform: async (action, input) => {
        window.operations.push({ action, input: structuredClone(input) })
        await new Promise(resolve => setTimeout(resolve, 20))
        if (action === "get") return { ok: true, draft: structuredClone(window.savedDraft) }
        if (input.revision !== window.savedDraft.revision) {
          const error = new Error("conflict"); error.code = "REVISION_CONFLICT"; throw error
        }
        const next = structuredClone(window.savedDraft)
        if (action === "update") {
          for (const [key, value] of Object.entries(input.descriptors ?? {})) {
            if (value == null) { delete next.descriptors[key]; delete next.descriptor_sources[key] }
            else { next.descriptors[key] = value; next.descriptor_sources[key] = "user" }
          }
          for (const [key, value] of Object.entries(input.parameters ?? {})) next.parameters[key] = value ?? next.parameter_fields[key].default
          next.revision++
        } else if (action === "reference") {
          next.descriptors = structuredClone(all)
          next.descriptor_sources = Object.fromEntries(names.map(name => [name, "reference"]))
          next.reference_smiles = input.reference_smiles; next.canonical_smiles = "*CC*"; next.revision++
        } else if (action === "confirm") {
          next.status = "submitted"; next.editable = false; next.job_id = "polytao.existing.task"
          if (window.failConfirm) {
            window.savedDraft = next
            throw new Error("Lost confirmation acknowledgement")
          }
        } else if (action === "cancel") { next.status = "cancelled"; next.editable = false }
        next.missing_fields = names.filter(name => !Object.hasOwn(next.descriptors, name))
        next.ready = next.missing_fields.length === 0
        window.savedDraft = next
        return { ok: true, draft: structuredClone(next), continuation: { status: action === "confirm" ? "started" : "not_needed" } }
      }, onChange: () => window.onChangeCount++,
    })
    document.querySelector("main").append(window.formPanel)
  }, { initial, names: NAMES, all: complete() })
  await page.getByRole("button", { name: "保存参数", exact: true }).waitFor()
  await page.waitForFunction(() => document.querySelector("[data-polytao-draft]").getAttribute("aria-busy") === "false")
}

async function waitIdle(page) {
  await page.waitForFunction(() => document.querySelector("[data-polytao-draft]").getAttribute("aria-busy") === "false")
}

test("browser draft input, reference calculation and saved refresh never submit before explicit confirmation", browserOptions, async () => {
  await browserHarness(async page => {
    await mountForm(page)
    const panel = page.locator("[data-polytao-draft]")
    assert.equal(await panel.locator("[data-descriptor-name]").count(), 15)
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get"])
    assert.equal(await panel.locator(".nexpoly-draft-advanced").getAttribute("open"), null)
    await panel.locator(".nexpoly-draft-reference > summary").click()
    await panel.locator("[data-polytao-reference]").fill("*CC*")
    await page.getByRole("button", { name: "计算并替换全部描述符", exact: true }).click()
    await waitIdle(page)
    assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "update", "reference"])
    assert.equal(await panel.locator('[data-descriptor-name="HeavyAtomCount"]').inputValue(), "0")
    assert.match(await panel.locator('[data-descriptor-name="HeavyAtomCount"]').locator("..").innerText(), /参考结构计算/)
    const molwt = panel.locator('[data-descriptor-name="MolWt"]')
    await molwt.fill("")
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    await page.getByRole("button", { name: "保存参数", exact: true }).click()
    await waitIdle(page)
    assert.equal(await page.evaluate(() => window.operations.at(-1).input.descriptors.MolWt), null)
    assert.equal(await page.evaluate(() => Object.hasOwn(window.savedDraft.descriptors, "MolWt")), false)
    await molwt.fill("28.05")
    await page.getByRole("button", { name: "保存参数", exact: true }).click()
    await waitIdle(page)
    await page.evaluate(() => {
      const old = structuredClone(window.savedDraft)
      window.formPanel.remove()
      window.refreshedDraft = old
    })
    await mountForm(page, await page.evaluate(() => window.refreshedDraft))
    assert.equal(await page.locator('[data-descriptor-name="MolWt"]').inputValue(), "28.05")
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isEnabled(), true)
    assert.equal(await page.evaluate(() => window.operations.some(item => item.action === "confirm")), false)
    for (const width of [320, 452, 768]) {
      await page.setViewportSize({ width, height: 1000 })
      const overflow = await page.locator("[data-polytao-draft]").evaluate(node => node.scrollWidth - node.clientWidth)
      assert.ok(overflow <= 1, `No form overflow at ${width}px`)
    }
  })
})

test("browser confirmation saves and acknowledges current inputs once, then resumes only the accepted job", browserOptions, async () => {
  await browserHarness(async page => {
    await mountForm(page, draft({ descriptors: complete() }))
    await page.locator('[data-descriptor-name="MolWt"]').fill("99.5")
    await page.getByRole("button", { name: "确认并生成", exact: true }).evaluate(button => { button.click(); button.click() })
    await waitIdle(page)
    const operations = await page.evaluate(() => window.operations)
    assert.deepEqual(operations.map(item => item.action), ["get", "update", "confirm"])
    assert.equal(operations[1].input.descriptors.MolWt, 99.5)
    assert.equal(operations[2].input.revision, operations[1].input.revision + 1)
    assert.equal(await page.evaluate(() => window.onChangeCount), 1)
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    assert.match(await page.locator("[data-polytao-draft-receipt]").innerText(), /polytao.existing.task/)
    await page.getByRole("button", { name: "继续查询结果", exact: true }).click()
    await waitIdle(page)
    assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "update", "confirm", "continue"])
  })
})

test("browser edits survive passive newer snapshots and require reload on a conflicting revision", browserOptions, async () => {
  await browserHarness(async page => {
    await mountForm(page, draft({ descriptors: complete() }))
    const molwt = page.locator('[data-descriptor-name="MolWt"]')
    await molwt.fill("77")
    await page.evaluate(() => {
      const next = structuredClone(window.savedDraft)
      next.revision++
      next.descriptors.MolWt = 88
      window.savedDraft = next
      window.formPanel.nexpolySetDraft(next)
    })
    assert.equal(await molwt.inputValue(), "77")
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    assert.match(await page.locator(".nexpoly-draft-notice").innerText(), /其他位置更新/)
    await page.getByRole("button", { name: "重新载入已保存参数", exact: true }).click()
    await waitIdle(page)
    assert.equal(await molwt.inputValue(), "88")
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isEnabled(), true)
    await molwt.fill("1e400")
    assert.match(await page.locator('[data-field-error="MolWt"]').innerText(), /有限数值/)
    assert.equal(await page.getByRole("button", { name: "保存参数", exact: true }).isDisabled(), true)
  })
})

test("a lost confirmation reply never retries submission and reload recovers the durable job receipt", browserOptions, async () => {
  await browserHarness(async page => {
    await mountForm(page, draft({ descriptors: complete() }))
    await page.evaluate(() => { window.failConfirm = true })
    await page.getByRole("button", { name: "确认并生成", exact: true }).click()
    await waitIdle(page)
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "confirm"])
    assert.match(await page.locator(".nexpoly-draft-notice").innerText(), /不会自动重复提交/)
    await page.getByRole("button", { name: "重新载入已保存参数", exact: true }).click()
    await waitIdle(page)
    assert.match(await page.locator("[data-polytao-draft-receipt]").innerText(), /polytao.existing.task/)
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    assert.equal(await page.evaluate(() => window.operations.filter(item => item.action === "confirm").length), 1)
  })
})

test("browser draft cancellation creates no generation request and untrusted labels remain text", browserOptions, async () => {
  await browserHarness(async page => {
    const value = draft()
    value.descriptor_fields[0].label = '<img src=x onerror="window.injected=true">'
    await mountForm(page, value)
    assert.equal(await page.locator("[data-polytao-draft] img, [data-polytao-draft] script").count(), 0)
    await page.getByRole("button", { name: "取消草稿", exact: true }).click()
    await waitIdle(page)
    assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "cancel"])
    assert.match(await page.locator("[data-polytao-draft-status]").innerText(), /已取消/)
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false)
  })
})

test("restored confirmed drafts recover through the same revision without changing values or issuing a new draft", browserOptions, async () => {
  await browserHarness(async page => {
    await mountForm(page, draft({ descriptors: complete(), status: "confirmed", editable: false }))
    assert.equal(await page.getByRole("button", { name: "确认并生成", exact: true }).isDisabled(), true)
    await page.getByRole("button", { name: "恢复已确认任务", exact: true }).click()
    await waitIdle(page)
    assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "confirm"])
    assert.equal(await page.evaluate(() => window.operations.at(-1).input.revision), 1)
    assert.equal(await page.getByRole("button", { name: "继续查询结果", exact: true }).isVisible(), true)
  })
})

test("untouched reference descriptors retain their source when saving or confirming unrelated edits", browserOptions, async () => {
  await browserHarness(async page => {
    const descriptors = complete()
    await mountForm(page, draft({ descriptors, descriptor_sources: Object.fromEntries(NAMES.map(name => [name, "reference"])) }))
    await page.locator('[data-parameter-name="candidate_count"]').fill("4")
    await page.getByRole("button", { name: "保存参数", exact: true }).click()
    await waitIdle(page)
    assert.deepEqual(await page.evaluate(() => window.operations.at(-1).input.descriptors), {})
    assert.equal(await page.evaluate(() => window.savedDraft.descriptor_sources.MolWt), "reference")
    await page.locator('[data-descriptor-name="MolWt"]').fill("30")
    await page.getByRole("button", { name: "确认并生成", exact: true }).click()
    await waitIdle(page)
    assert.equal(await page.evaluate(() => window.savedDraft.descriptor_sources.MolWt), "user")
    assert.equal(await page.evaluate(() => window.savedDraft.descriptor_sources.RingCount), "reference")
  })
})

test("conversation controller keeps one live draft across turns and recovers it through the bound same-origin bridge", browserOptions, async () => {
  await browserHarness(async page => {
    let saved = draft()
    let session = messages([tool(saved)])
    const requests = []
    await page.route("**/openscience-api/**", async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith("/message")) return route.fulfill({ json: session })
      assert.equal(url.pathname, "/openscience-api/nexpoly/polytao/draft")
      assert.equal(request.method(), "POST")
      assert.equal(request.headers().authorization, undefined)
      const body = request.postDataJSON()
      requests.push(body)
      assert.equal(body.session_id, "ses_current")
      assert.equal(body.directory, "/openscience/files/general")
      assert.equal(body.draft_id, DRAFT_ID)
      assert.equal(body.action, "get")
      return route.fulfill({ json: { ok: true, draft: saved, continuation: { status: "not_needed" } } })
    })
    await page.evaluate(async first => {
      history.replaceState({}, "", "/chat/ses_current")
      window.addTurn = user => {
        const node = document.createElement("div")
        node.dataset.messageId = user
        node.innerHTML = '<div data-component="session-turn"><div data-slot="session-turn-content"><div data-slot="session-turn-response">正常回答</div></div></div>'
        document.querySelector("main").append(node)
      }
      window.addTurn("user-one")
      await import("/results-controller.mjs")
      await fetch("/openscience-api/session/ses_current/message", { headers: { "x-openscience-directory": "/openscience/files/general" } })
    }, session)
    const panel = page.locator("[data-polytao-draft]")
    await panel.waitFor()
    await waitIdle(page)
    assert.equal(await panel.count(), 1)
    assert.equal(requests.length, 1)
    await page.locator('[data-descriptor-name="MolWt"]').fill("77")
    saved = draft({ revision: 2, descriptors: { MolWt: 88 }, updated_at: "2026-11-16T01:02:00Z" })
    session = [...session, ...messages([tool(saved, "get_polytao_draft", { draft_id: DRAFT_ID })], "user-two")]
    await page.evaluate(async () => {
      window.addTurn("user-two")
      await fetch("/openscience-api/session/ses_current/message", { headers: { "x-openscience-directory": "/openscience/files/general" } })
    })
    await page.waitForFunction(() => document.querySelector(".nexpoly-draft-notice").textContent.includes("其他位置更新"))
    assert.equal(await panel.count(), 1)
    assert.equal(await page.locator('[data-descriptor-name="MolWt"]').inputValue(), "77")
    assert.equal(await panel.locator("xpath=ancestor::*[@data-message-id]").getAttribute("data-message-id"), "user-one")
    await page.getByRole("button", { name: "重新载入已保存参数", exact: true }).click()
    await waitIdle(page)
    assert.equal(await page.locator('[data-descriptor-name="MolWt"]').inputValue(), "88")
    await page.locator('[data-descriptor-name="MolWt"]').fill("66")
    session = [...session, ...messages([{ type: "text", text: JSON.stringify(saved) }], "user-three")]
    await page.evaluate(async () => {
      window.addTurn("user-three")
      await fetch("/openscience-api/session/ses_current/message", { headers: { "x-openscience-directory": "/openscience/files/general" } })
    })
    await page.waitForTimeout(200)
    assert.equal(await panel.count(), 1)
    assert.equal(await page.locator('[data-descriptor-name="MolWt"]').inputValue(), "66")
    assert.equal(requests.every(request => request.action === "get"), true, "Passive conversation refresh cannot generate")
  })
})

test("legacy PolyTAO draft history remains readable while gateway prefixes are exact", () => {
  const part = tool(draft())
  const legacy = { ...part, tool: "nexpoly_polytao_prepare_polytao_generation" }
  assert.equal(extractPolytaoDrafts(messages([legacy])).length, 1)
  for (const name of ["capability_gateway_prepare_polytao_generation_extra", "capability_gateway_search_polymers", "fake_capability_gateway_prepare_polytao_generation"]) {
    assert.deepEqual(extractPolytaoDrafts(messages([{ ...part, tool: name }])), [])
  }
})
