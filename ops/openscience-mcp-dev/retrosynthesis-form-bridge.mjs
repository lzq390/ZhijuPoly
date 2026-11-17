/** Retrosynthesis has its own tool and request contract; it shares only the bridge mechanics. */
import { validateCapabilityDraftAction, capabilitySessionContainsDraft,
  createCapabilityUiMcpCaller, createCapabilityFormBoundary } from "./capability-form-bridge.mjs"
const object = value => value !== null && typeof value === "object" && !Array.isArray(value)
const TARGET_FIELDS = ["source", "smiles", "name", "record_id", "query", "cursor", "limit"]
const PARAMETERS = ["target_role", "num_return_sequences", "num_beams", "max_new_tokens"]
export function validRetrosynthesisTarget(value) {
  if (!object(value) || !["smiles", "name", "saved_structure"].includes(value.source)
      || Object.keys(value).some(key => !TARGET_FIELDS.includes(key))
      || !["smiles", "name", "record_id", "query", "cursor"].every(key => value[key] === undefined
        || typeof value[key] === "string" && value[key].length <= (key === "smiles" ? 8000 : key === "cursor" ? 512 : 1000))
      || value.limit !== undefined && (!Number.isSafeInteger(value.limit) || value.limit < 1 || value.limit > 20)) return false
  const supplied = ["smiles", "name", "record_id", "query", "cursor"].filter(key => value[key] !== undefined)
  const allowed = { smiles: ["smiles"], name: ["name"], saved_structure: ["record_id", "query", "cursor"] }[value.source]
  if (supplied.some(key => !allowed.includes(key)) || value.source === "smiles" && !value.smiles?.trim()
      || value.source === "name" && !value.name?.trim()) return false
  if (value.record_id !== undefined && (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value.record_id)
      || value.query !== undefined || value.cursor !== undefined)) return false
  return true
}
export const RETROSYNTHESIS_FORM_ADAPTER = Object.freeze({
  route: "/nexpoly/retrosynthesis/draft", draftPattern: /^rtd_[a-f0-9]{32}$/,
  actions: ["get", "resolve", "update", "cancel", "confirm", "continue"],
  extraFields: { update: ["target", "parameters"], resolve: ["target"] },
  prepareTools: ["capability_gateway_prepare_monomer_retrosynthesis"],
  receiptKey: "nexpoly_retrosynthesis_receipt",
  uiTools: ["resolve_retrosynthesis_target", "get_retrosynthesis_draft", "update_retrosynthesis_draft",
    "confirm_monomer_retrosynthesis", "cancel_retrosynthesis_draft"],
  validExtra(body) {
    if (body.action === "resolve" && !validRetrosynthesisTarget(body.target)) return false
    if (body.target !== undefined && body.target !== null && !validRetrosynthesisTarget(body.target)) return false
    return body.parameters === undefined || object(body.parameters)
      && Object.keys(body.parameters).every(key => PARAMETERS.includes(key))
      && Object.entries(body.parameters).every(([key, value]) => value === null
        || (key === "target_role" ? ["auto", "diamine", "dianhydride", "other"].includes(value) : Number.isSafeInteger(value)))
  },
  call(body) {
    if (body.action === "resolve") return { name: "resolve_retrosynthesis_target", args: { target: body.target } }
    const args = { draft_id: body.draft_id }
    let name = "get_retrosynthesis_draft"
    if (["update", "confirm", "cancel"].includes(body.action)) args.revision = body.revision
    if (body.action === "update") {
      name = "update_retrosynthesis_draft"
      if (body.target !== undefined) args.target = body.target
      if (body.parameters !== undefined) args.parameters = body.parameters
    } else if (body.action === "confirm") name = "confirm_monomer_retrosynthesis"
    else if (body.action === "cancel") name = "cancel_retrosynthesis_draft"
    return { name, args }
  },
  receiptText: "用户已在参数板确认并提交单体逆合成任务。以下是服务端通过 MCP 返回的提交收据。"
    + "仅使用 get_retrosynthesis_job 查询该 job_id 并展示单步前体候选；不得重新准备或提交任务。",
})
export const validateRetrosynthesisDraftAction = body => validateCapabilityDraftAction(RETROSYNTHESIS_FORM_ADAPTER, body)
export const retrosynthesisSessionContainsDraft = (messages, sessionId, draftId) => capabilitySessionContainsDraft(RETROSYNTHESIS_FORM_ADAPTER, messages, sessionId, draftId)
export const createRetrosynthesisUiMcpCaller = options => createCapabilityUiMcpCaller(RETROSYNTHESIS_FORM_ADAPTER, options)
export const createRetrosynthesisFormBoundary = options => createCapabilityFormBoundary(RETROSYNTHESIS_FORM_ADAPTER, options)
