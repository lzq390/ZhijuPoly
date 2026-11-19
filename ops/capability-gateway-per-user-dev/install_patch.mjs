// Keep the production schemas, response transformations, tool switch and MCP transport.
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";

const expected = {
  "src/auth.ts": "b5320c5f1532d9eca15e3c57de7b2d66dfd6c0bd2eb8957d798ed98f34cb5c9d",
  "src/errors.ts": "84d0632459638ad3615b97f38fe970b66cdcebb92e8a3c6b7800f23f2e133e4f",
  "src/log.ts": "17d792c45888b768bcb861dcbc51a025c524266d747c246d59367a6f074d248a",
  "src/mcp.ts": "8e3d56f40cb0bb1f7234321d24a8d7ccee244ecda4fec50d20fa7c2a96beeeb4",
  "src/server.ts": "d2aee13395a2e022a25d6807b5928824c43963d8775084275b2b074ed24f5bf4",
  "src/tools.ts": "16ef019a594a99db927556d8cad453c864641d722ce24d9bc19b6cc2edb44431",
  "src/zhiju-client.ts": "82ead7d766df9a947463b4260e599e813116bf4433a2b6e70cf2d4e92adb8be3",
  "package.json": "70b9016c49cbcb32d673995674960c5b62562e9dc955c1286cfaaeec578df2f6",
  "bun.lock": "71b193b9e5385add906a36769afde0967bf0222a571b6bd40beec08560cf6eda",
};
const source = {};
for (const [name, hash] of Object.entries(expected)) {
  source[name] = await readFile(name, "utf8");
  if (createHash("sha256").update(source[name]).digest("hex") !== hash)
    throw Error("The local gateway differs from its reviewed November input: " + name);
}

function once(input, match, replacement) {
  if (input.split(match).length !== 2) throw Error("Unexpected upstream anchor");
  return input.replace(match, replacement);
}
function section(input, start, end, replacement) {
  if (input.split(start).length !== 2 || input.split(end).length !== 2 || input.indexOf(start) >= input.indexOf(end))
    throw Error("Unexpected upstream section");
  return input.slice(0, input.indexOf(start)) + replacement + input.slice(input.indexOf(end));
}

let client = section(source["src/zhiju-client.ts"], "// 用户可访问的前端页面地址", "  private async requestDft",
  'export class ZhijuClient extends ToolDelegation {\n');
client = once(client, 'import { logEvent, redact } from "./log";',
  'import { logEvent } from "./log";\nimport { ToolDelegation } from "./delegation";');
client = once(client, '      headers: requestBody === undefined ? {} : { "Idempotency-Key": crypto.randomUUID() },\n', "");
client = once(client, "frontendOriginFor(this.baseUrl)", "this.frontendOrigin");
await writeFile("src/zhiju-client.ts", client);

let mcp = section(source["src/mcp.ts"], "let sharedClient:", "  const server = new Server(",
  "export async function handleMcpRequest(request: Request): Promise<Response> {\n");
mcp = once(mcp, "    const start = performance.now();",
  "    const zhijuClient = new ZhijuClient(request, params.name, params.arguments ?? {});\n    const start = performance.now();");
mcp = once(mcp, "server.onerror = error =>", "server.onerror = () =>");
mcp = once(mcp, "error: zhijuClient.redactForLog(error),", 'error: "Invalid MCP message",');
await writeFile("src/mcp.ts", mcp);

let server = once(source["src/server.ts"], 'import { createAuthenticator } from "./auth";',
  'import { createAuthenticator } from "./auth";\nimport { serviceKey } from "./delegation";');
server = once(server, "const authenticated = createAuthenticator(env);",
  'const authenticated = createAuthenticator({ ZHIJU_CAPABILITY_TOKEN: serviceKey("OPENSCIENCE_M3_INGRESS_KEY_FILE") });');
server = once(server, 'if (!authenticated(request.headers.get("Authorization")))',
  'if (request.headers.has("Origin") || request.headers.has("Cookie") || !authenticated(request.headers.get("Authorization")))');
server = once(server, 'hostname: process.env.LISTEN_HOST ?? "127.0.0.1",\n    port: 3100,',
  'unix: process.env.OPENSCIENCE_M3_GATEWAY_SOCKET ?? "/ipc/gateway.sock",');
await writeFile("src/server.ts", server);
