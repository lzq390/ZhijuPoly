/** PolyTAO's typed adapter over the common, server-only confirmation bridge. */
import { validateCapabilityDraftAction, capabilitySessionContainsDraft,
  createCapabilityUiMcpCaller, createCapabilityFormBoundary } from "./capability-form-bridge.mjs"
const object = value => value !== null && typeof value === "object" && !Array.isArray(value)
export const POLYTAO_FORM_ADAPTER = Object.freeze({
  route: "/nexpoly/polytao/draft", draftPattern: /^ptd_[a-f0-9]{32}$/,
  actions: ["get", "update", "reference", "cancel", "confirm", "continue"],
  extraFields: { update: ["descriptors", "parameters"], reference: ["reference_smiles"] },
  prepareTools: ["nexpoly_polytao_prepare_polytao_generation", "capability_gateway_prepare_polytao_generation"],
  receiptKey: "nexpoly_polytao_receipt",
  uiTools: ["get_polytao_draft", "update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft"],
  validExtra: body => (body.descriptors === undefined || object(body.descriptors))
    && (body.parameters === undefined || object(body.parameters))
    && (body.action !== "reference" || body.reference_smiles === null
      || typeof body.reference_smiles === "string" && body.reference_smiles.length <= 8192),
  call(body) {
    const args = { draft_id: body.draft_id }
    let name = "get_polytao_draft"
    if (["update", "reference", "confirm", "cancel"].includes(body.action)) args.revision = body.revision
    if (body.action === "update") {
      name = "update_polytao_draft"
      if (body.descriptors !== undefined) args.descriptors = body.descriptors
      if (body.parameters !== undefined) args.parameters = body.parameters
    } else if (body.action === "reference") { name = "update_polytao_draft"; args.reference_smiles = body.reference_smiles }
    else if (body.action === "confirm") name = "confirm_polytao_generation"
    else if (body.action === "cancel") name = "cancel_polytao_draft"
    return { name, args }
  },
  receiptText: "用户已在参数卡片中确认并提交聚合物生成。以下是服务端通过 MCP 返回的提交收据。"
    + "只查询该 job_id 并展示结果；不得重新准备或提交生成任务。",
})
export const validateDraftAction = body => validateCapabilityDraftAction(POLYTAO_FORM_ADAPTER, body)
export const sessionContainsDraft = (messages, sessionId, draftId) => capabilitySessionContainsDraft(POLYTAO_FORM_ADAPTER, messages, sessionId, draftId)
export const createUiMcpCaller = options => createCapabilityUiMcpCaller(POLYTAO_FORM_ADAPTER, options)
export const createPolytaoFormBoundary = options => createCapabilityFormBoundary(POLYTAO_FORM_ADAPTER, options)
