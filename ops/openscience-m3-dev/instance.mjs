// Only the trusted router supplies the handle. It never enters message parts, model input or MCP arguments.
import { AsyncLocalStorage } from "node:async_hooks";

const requests = new AsyncLocalStorage();
const forms = new AsyncLocalStorage();
const turns = new Map();
const ttl = 15 * 60_000;
export const withToolTurn = (c, next) =>
  forms.run(c.req.header("x-nexpoly-form-action"), () =>
    requests.run(c.req.header("x-nexpoly-tool-turn"), next),
  );

export function rememberTurn(session, message) {
  const handle = requests.getStore();
  if (!handle) return;
  const now = Date.now();
  for (const [key, entry] of turns) if (entry.until <= now) turns.delete(key);
  turns.set(session + "/" + message, { handle, until: now + ttl });
}

export async function toolsForTurn(input, jsonSchema) {
  const session = input.session.id,
    turn = input.processor.message.parentID;
  const entry = turns.get(session + "/" + turn);
  if (!entry || entry.until <= Date.now()) return {};
  async function rpc(path, body) {
    const r = await fetch("http://localhost/" + path, {
      unix: "/m3-peer/tools.sock",
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer " + entry.handle },
      body: JSON.stringify({ session, turn, ...body }),
      signal: AbortSignal.timeout(55000),
    });
    if (!r.ok) throw Error("OpenScience tool authorization or service unavailable (" + r.status + ")");
    return r.json();
  }
  const catalog = await rpc("catalog", {});
  const tools = {};
  for (const definition of catalog.tools) {
    const name = definition.name,
      key = "capability_gateway_" + name;
    tools[key] = {
      type: "dynamic",
      description: definition.description,
      inputSchema: jsonSchema(definition.inputSchema),
      execute: async (args, opts) => rpc("call", { call: opts.toolCallId, tool: name, arguments: args }),
      // Prepare authorization before the native permission prompt. The router reads the
      // actual pending permission back from this instance when the browser answers it.
      prepare: (args, opts) => rpc("prepare", { call: opts.toolCallId, tool: name, arguments: args }),
    };
  }
  return tools;
}

// The instance can redeem this short-lived handle only for the action that the browser already requested.
export async function formTool(name, args) {
  const handle = forms.getStore();
  if (!handle) throw Error("Missing authenticated form context");
  const r = await fetch("http://localhost/form", {
    unix: "/m3-peer/tools.sock",
    method: "POST",
    headers: { authorization: "Bearer " + handle, "content-type": "application/json" },
    body: JSON.stringify({ tool: name, arguments: args }),
    signal: AbortSignal.timeout(28000),
  });
  if (!r.ok) throw Error("Development form transport unavailable");
  return r.json();
}
