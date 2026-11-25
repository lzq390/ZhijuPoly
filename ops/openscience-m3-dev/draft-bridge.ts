// These imports resolve inside the reviewed November workspace.
import { Session } from "/app/backend/cli/src/session/index.ts";
import { MessageV2 } from "/app/backend/cli/src/session/message-v2.ts";
import { Instance } from "/app/backend/cli/src/project/instance.ts";
import { InstanceBootstrap } from "/app/backend/cli/src/project/bootstrap.ts";
import { Storage } from "/app/backend/cli/src/storage/storage.ts";
import { workspacePath } from "/app/backend/cli/src/server/nexpoly-dev-boundary.mjs";
import { POLYTAO_FORM_ADAPTER } from "/app/backend/cli/src/server/polytao-form-bridge.mjs";
import { RETROSYNTHESIS_FORM_ADAPTER } from "/app/backend/cli/src/server/retrosynthesis-form-bridge.mjs";
import { draftEvidence, matchingReceipt, validateCapabilityDraftAction } from "/app/backend/cli/src/server/capability-form-bridge.mjs";
import { createDraftHistory } from "./draft-history.mjs";

const lookup = createDraftHistory({
  getSession: Session.get, getMessage: MessageV2.get, stream: MessageV2.stream,
  findPart: draftEvidence, receiptFor: matchingReceipt,
});
const adapters = { polytao: POLYTAO_FORM_ADAPTER, retrosynthesis: RETROSYNTHESIS_FORM_ADAPTER };

export const draftMessages = (adapter, body) =>
  lookup(adapter, body, body.action === "confirm" || body.action === "continue");

export async function latestUser(session) {
  for await (const message of MessageV2.stream(session)) {
    if (message.info.role === "user") return message.info;
  }
}

// This route is available on the instance socket only. The public router
// accepts only the two existing form URLs and checks the login and generation.
export async function draftProof(c) {
  const name = c.req.param("adapter");
  if (!Object.hasOwn(adapters, name)) return c.body(null, 404);
  const adapter = adapters[name];
  const body = validateCapabilityDraftAction(adapter, await c.req.json());
  const directory = await workspacePath(body.directory, "");
  try {
    return await Instance.provide({ directory, init: InstanceBootstrap, fn: async () => {
      const messages = await lookup(adapter, body, false);
      if (!messages) return c.body(null, 404);
      const { info, parts } = messages[0];
      return c.json({ session: info.sessionID, draft: body.draft_id, turn: info.parentID, call: parts[0].callID });
    } });
  } catch (error) {
    if (error instanceof Storage.NotFoundError) return c.body(null, 404);
    throw error;
  }
}
