// Both model and browser operations are scoped by the authenticated workspace router.
import { readFile } from "node:fs/promises";
import { POLYTAO_FORM_ADAPTER } from "../openscience-mcp-dev/polytao-form-bridge.mjs";
import { RETROSYNTHESIS_FORM_ADAPTER } from "../openscience-mcp-dev/retrosynthesis-form-bridge.mjs";
import {
  validateCapabilityDraftAction,
} from "../openscience-mcp-dev/capability-form-bridge.mjs";

const adapters = { polytao: POLYTAO_FORM_ADAPTER, retrosynthesis: RETROSYNTHESIS_FORM_ADAPTER };
const modelTools = {
  polytao: [
    "get_polytao_capabilities",
    "calculate_polytao_descriptors",
    "prepare_polytao_generation",
    "get_polytao_draft",
    "get_polytao_job",
  ],
  retrosynthesis: [
    "get_retrosynthesis_capabilities",
    "resolve_retrosynthesis_target",
    "prepare_monomer_retrosynthesis",
    "get_retrosynthesis_draft",
    "get_retrosynthesis_job",
  ],
};
export const formAdapter = (path) => Object.keys(adapters).find((key) => adapters[key].route === path);
export const modelAdapter = (tool) => Object.keys(adapters).find((key) => modelTools[key].includes(tool));

export async function connectAdapters(config, unix) {
  const endpoints = {};
  for (const name of Object.keys(adapters)) {
    const { socket, ingress_file } = config[name];
    endpoints[name] = { socket, key: (await readFile(ingress_file, "utf8")).trim() };
  }
  async function rpc(name, method, params, grant) {
    const endpoint = endpoints[name];
    const msg = await unix(
      endpoint.socket,
      "POST",
      "/mcp",
      {
        authorization: "Bearer " + endpoint.key,
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        host: "localhost",
        ...(grant ? { "x-openscience-operation": grant } : {}),
      },
      JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    );
    if (msg.error || !msg.result) throw Error("Adapter protocol error");
    return msg.result;
  }
  async function catalog() {
    const catalogs = await Promise.all(
      Object.keys(adapters).map(async (name) => {
        const { tools } = await rpc(name, "tools/list", {});
        if (
          JSON.stringify(tools.map((t) => t.name).sort()) !== JSON.stringify([...modelTools[name]].sort())
        ) {
          throw Error("Unexpected development adapter tool catalog");
        }
        return tools;
      }),
    );
    return catalogs.flat();
  }
  async function formEvidence(name, payload, page, credential) {
    const adapter = adapters[name];
    if (payload.length > 32768) throw Object.assign(Error("draft_action_too_large"), { status: 413 });
    let body;
    try {
      body = JSON.parse(payload);
    } catch {
      throw Object.assign(Error("invalid_draft_json"), { status: 400, code: "INVALID_ACTION" });
    }
    const action = validateCapabilityDraftAction(adapter, body);
    const session = action.session_id;
    const absent = () =>
      Object.assign(Error("draft_not_in_session"), { status: 403, code: "DRAFT_NOT_IN_SESSION" });
    // The private workspace resolves against canonical records. Large/private
    // message bodies never cross this socket just to authorize a button.
    const proof = await unix(page.route.socket, "POST", "/nexpoly/private/draft-evidence/" + name,
      { authorization: "Bearer " + credential, "content-type": "application/json" }, payload).catch((error) => {
      if (error.status === 403 || error.status === 404) throw absent();
      throw error;
    });
    if (proof.session !== session || proof.draft !== action.draft_id ||
        !/^msg_[A-Za-z0-9_-]{1,120}$/.test(proof.turn || "") || !/^[A-Za-z0-9_-]{1,128}$/.test(proof.call || "")) throw absent();
    const { name: tool, args } = adapter.call(action);
    return {
      name,
      tool,
      args: { ...args, conversation_id: session },
      session,
      turn: proof.turn,
      call: proof.call,
      draft_id: action.draft_id,
      revision: action.revision,
      confirmed: action.action === "confirm",
    };
  }
  return { rpc, catalog, formEvidence };
}
