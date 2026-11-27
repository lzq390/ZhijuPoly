import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createDraftHistory, rememberPreparation } from "./draft-history.mjs";
import { connectAdapters } from "./adapter-router.mjs";
import { draftEvidence, matchingReceipt } from "../openscience-mcp-dev/capability-form-bridge.mjs";
import { POLYTAO_FORM_ADAPTER } from "../openscience-mcp-dev/polytao-form-bridge.mjs";
import { RETROSYNTHESIS_FORM_ADAPTER } from "../openscience-mcp-dev/retrosynthesis-form-bridge.mjs";

for (const [name, adapter, tool, prefix] of [
  ["polytao", POLYTAO_FORM_ADAPTER, "prepare_polytao_generation", "ptd_"],
  ["retrosynthesis", RETROSYNTHESIS_FORM_ADAPTER, "prepare_monomer_retrosynthesis", "rtd_"],
]) {
  test(name + " reopens an old draft and receipt beyond 1,000 messages, then uses checked locators", async t => {
    const directory = await mkdtemp(join(tmpdir(), "draft-location-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const session = "ses_retained", project = "/openscience/files/projects/same";
    const draft = prefix + "a".repeat(32);
    const body = { session_id: session, directory: project, draft_id: draft, action: "confirm", revision: 1 };
    const output = { ok: true, draft_id: draft, conversation_id: session };
    const make = (id, role, parts) => ({ info: { sessionID: session, id, role, parentID: "msg_user" }, parts });
    const prep = make("msg_prepare", "assistant", [
      { tool: "capability_gateway_" + tool, type: "tool", callID: "call_first",
        state: { status: "completed", output: JSON.stringify(output) } },
      { type: "text", text: "a".repeat(2 * 1024 * 1024) },
    ]);
    const receipt = make("msg_receipt", "user", [{ type: "text", synthetic: true,
      metadata: { [adapter.receiptKey]: { ...output, job_id: "original" } } }]);
    const transcript = [prep, receipt, ...Array.from({ length: 1002 }, (_, i) =>
      make("msg_later" + i, "assistant", [{ type: "text", text: "unrelated".repeat(300) }]))];
    const records = new Map(transcript.map(message => [message.info.id, message]));
    let visited = 0;
    const store = { directory, getSession: async id => ({ id, directory: project }),
      getMessage: async ({ messageID }) => records.get(messageID),
      stream: async function* () {
        for (let i = transcript.length - 1; i >= 0; i--) { visited++; yield transcript[i]; }
      }, findPart: draftEvidence, receiptFor: matchingReceipt };
    const lookup = createDraftHistory(store);
    const legacy = await lookup(adapter, body, true);
    assert.equal(visited, transcript.length);
    assert.equal(legacy[0].parts.length, 1);
    assert.equal(matchingReceipt(adapter, legacy, body).job_id, "original");
    assert.equal(legacy.at(-1).info.id, transcript.at(-1).info.id);
    assert.ok(JSON.stringify(legacy).length < 1500);

    visited = 0;
    const restarted = createDraftHistory(store);
    assert.equal((await restarted(adapter, body, false))[0].parts[0].callID, "call_first");
    assert.equal(visited, 0);
    assert.equal(matchingReceipt(adapter, await restarted(adapter, body, true), body).job_id, "original");
    assert.equal(visited, 1);
    assert.equal(await restarted(adapter, { ...body, directory: project + "-other" }, false), null);
    assert.equal(await restarted(adapter, { ...body, session_id: "ses_other" }, false), null);

    // Newly prepared drafts get a pointer directly from the trusted tool callback.
    await rm(join(directory, draft + ".prepare"));
    await rememberPreparation(tool, output, session, prep.info.id, "call_first", directory);
    visited = 0;
    assert.equal((await restarted(adapter, body, false))[0].info.id, prep.info.id);
    assert.equal(visited, 0);
    // A stale locator cannot turn text or an absent canonical part into authority.
    prep.parts[0].state.status = "error";
    assert.equal(await restarted(adapter, body, false), null);
  });

  test(name + " form sends a scoped proof request and binds the returned turn and call", async t => {
    const root = await mkdtemp(join(tmpdir(), "draft-form-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const file = join(root, "key");
    await writeFile(file, "local-ingress");
    const config = { socket: "adapter", ingress_file: file };
    const draft = prefix + "a".repeat(32);
    const body = { action: "confirm", session_id: "ses_original", directory: "/openscience/files/general",
      draft_id: draft, revision: 2 };
    let proof = { session: body.session_id, draft, turn: "msg_input", call: "call_1" };
    const api = await connectAdapters({ polytao: config, retrosynthesis: config }, async (socket, method, path, headers, payload) => {
      assert.deepEqual([socket, method, path, headers.authorization],
        ["owner-socket", "POST", "/nexpoly/private/draft-evidence/" + name, "Bearer owner-secret"]);
      assert.deepEqual(JSON.parse(payload), body);
      return proof;
    });
    const form = () => api.formEvidence(name, Buffer.from(JSON.stringify(body)), { route: { socket: "owner-socket" } }, "owner-secret");
    const record = await form();
    assert.deepEqual([record.turn, record.call, record.draft_id, record.revision, record.confirmed],
      ["msg_input", "call_1", draft, 2, true]);
    proof = { ...proof, session: "ses_other" };
    await assert.rejects(form, { status: 403 });
  });
}
