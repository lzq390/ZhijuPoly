// Locators are hints on the owner's private volume. Only the canonical saved
// tool part and the native session directory can authorize a form operation.
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { join } from "node:path";

const root = "/openscience/state/m3-draft-locations";
const draftId = /^(?:ptd|rtd)_[0-9a-f]{32}$/;
const messageId = /^msg_[A-Za-z0-9_-]{1,120}$/;
const prepares = { prepare_polytao_generation: "ptd_", prepare_monomer_retrosynthesis: "rtd_" };

async function saveLocation(directory, draft, kind, value) {
  if (!draftId.test(draft) || !messageId.test(value.message)) return;
  const path = join(directory, draft + "." + kind);
  const temp = path + "." + randomUUID();
  try {
    await mkdir(directory, { recursive: true, mode: 0o700 });
    await writeFile(temp, JSON.stringify(value), { flag: "wx", mode: 0o600 });
    await rename(temp, path);
  } catch {
    // A full volume must still allow lookups from the saved conversation.
  } finally {
    await unlink(temp).catch(() => {});
  }
}

export async function rememberPreparation(tool, output, session, message, call, directory = root) {
  if (Object.hasOwn(prepares, tool) && output?.ok === true && output.conversation_id === session
      && typeof output.draft_id === "string" && output.draft_id.startsWith(prepares[tool])) {
    await saveLocation(directory, output.draft_id, "prepare", { session, message, call });
  }
}

export function createDraftHistory({ getSession, getMessage, stream, findPart, receiptFor, directory = root }) {
  // stream() yields newest first. For older volumes without locators we keep
  // only the matching parts and stop at the original preparation.
  return async function lookup(adapter, body, includeReceipts) {
    const { session_id: session, draft_id: draft } = body;
    const info = await getSession(session);
    if (info.id !== session || info.directory !== body.directory || !draftId.test(draft)) return null;

    function preparation(message, call) {
      if (message?.info?.sessionID !== session) return null;
      const parts = call ? message.parts.filter(part => part.callID === call) : message.parts;
      const part = findPart(adapter, [{ info: message.info, parts }], session, draft);
      return part && { info: message.info, parts: [part] };
    }
    function receipt(message) {
      if (message?.info?.sessionID !== session || message.info.role !== "user") return null;
      const parts = message.parts.filter(part => receiptFor(adapter, [{ info: message.info, parts: [part] }], body));
      return parts.length ? { info: message.info, parts } : null;
    }
    async function fromLocation(kind, select) {
      try {
        const pin = JSON.parse(await readFile(join(directory, draft + "." + kind), "utf8"));
        if (pin.session !== session || !messageId.test(pin.message)) return null;
        const message = await getMessage({ sessionID: session, messageID: pin.message });
        if (message.info.id === pin.message) return select(message, pin.call);
      } catch { /* Rebuild missing or stale hints from the canonical records. */ }
      return null;
    }
    let prepared = await fromLocation("prepare", preparation);
    if (prepared && !includeReceipts) return [prepared];
    let savedReceipt = includeReceipts && await fromLocation("receipt", receipt);
    let latest;
    for await (const message of stream(session)) {
      if (message.info.sessionID !== session) continue;
      if (includeReceipts && !latest) {
        // pendingReadback examines only the newest message.
        latest = { info: message.info, parts: message.parts.filter(part => part.synthetic && part.metadata?.[adapter.continuationKey]) };
      }
      if (!prepared) {
        prepared = preparation(message);
        if (prepared) {
          await saveLocation(directory, draft, "prepare", { session, message: message.info.id, call: prepared.parts[0].callID });
        }
      }
      if (includeReceipts && !savedReceipt) {
        savedReceipt = receipt(message);
        if (savedReceipt) await saveLocation(directory, draft, "receipt", { session, message: message.info.id });
      }
      if (prepared && (!includeReceipts || savedReceipt || message.info.id === prepared.info.id)) break;
    }
    return prepared ? [prepared, savedReceipt, latest].filter(Boolean) : null;
  };
}
