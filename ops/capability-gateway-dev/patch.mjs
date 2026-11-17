import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { pathToFileURL } from "node:url"

export const EXPECTED_MCP_SHA256 = "8e3d56f40cb0bb1f7234321d24a8d7ccee244ecda4fec50d20fa7c2a96beeeb4"
export const EXPECTED_SERVER_SHA256 = "d2aee13395a2e022a25d6807b5928824c43963d8775084275b2b074ed24f5bf4"

export function patchGateway(source) {
  if (createHash("sha256").update(source).digest("hex") !== EXPECTED_MCP_SHA256) {
    throw new Error("Capability gateway source differs from the reviewed base")
  }
  const replace = (before, after) => {
    if (source.split(before).length !== 2) throw new Error("Capability gateway patch anchor is not unique")
    source = source.replace(before, after)
  }
  replace('import { logEvent } from "./log";',
    'import { logEvent } from "./log";\nimport { propertyFilterProxy, isPropertyFilterTool } from "./property-filter";\nimport { polytaoProxy, isPolytaoTool, isPolytaoAudienceTool } from "./polytao";\nimport type { PolytaoAudience } from "./polytao";')
  replace('export async function handleMcpRequest(request: Request): Promise<Response> {',
    'export async function handleMcpRequest(request: Request, audience: PolytaoAudience = "model"): Promise<Response> {')
  replace('  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));',
    `  // nexpoly-polytao-gateway-v1: every request carries its authenticated audience.
  server.setRequestHandler(ListToolsRequestSchema, async () => {
    if (audience === "ui") {
      try { return { tools: await polytaoProxy.listTools("ui") }; }
      catch {
        logEvent({ evt: "polytao_ui_registry_unavailable", error: "Polymer generation is temporarily unavailable" });
        return { tools: [] };
      }
    }
    const [filter, polytao] = await Promise.allSettled([
      propertyFilterProxy.listTools(), polytaoProxy.listTools("model"),
    ]);
    if (filter.status === "rejected")
      logEvent({ evt: "filter_registry_unavailable", error: "Property filtering is temporarily unavailable" });
    if (polytao.status === "rejected")
      logEvent({ evt: "polytao_registry_unavailable", error: "Polymer generation is temporarily unavailable" });
    return { tools: [...TOOLS,
      ...(filter.status === "fulfilled" ? filter.value : []),
      ...(polytao.status === "fulfilled" ? polytao.value : [])] };
  });`)
  replace('    try {\n      let result: unknown;',
    `    try {
      if ((audience === "ui" && !isPolytaoAudienceTool(params.name, "ui"))
          || (isPolytaoTool(params.name) && !isPolytaoAudienceTool(params.name, audience))) {
        return { isError: true, content: [{ type: "text", text: "This gateway credential cannot access this tool." }] };
      }
      if (isPolytaoTool(params.name)) {
        const result = await polytaoProxy.callTool(params.name, params.arguments ?? {}, audience);
        ok = !result.isError;
        return result;
      }
      if (isPropertyFilterTool(params.name)) {
        const result = await propertyFilterProxy.callTool(params.name, params.arguments ?? {});
        ok = !result.isError;
        // Return the full MCP result, including structuredContent, attachments and metadata.
        return result;
      }
      let result: unknown;`)
  return source
}

export function patchGatewayServer(source) {
  if (createHash("sha256").update(source).digest("hex") !== EXPECTED_SERVER_SHA256) {
    throw new Error("Capability gateway server differs from the reviewed base")
  }
  const replace = (before, after) => {
    if (source.split(before).length !== 2) throw new Error("Capability gateway server patch anchor is not unique")
    source = source.replace(before, after)
  }
  replace('import { logEvent, redact } from "./log";',
    'import { logEvent, redact } from "./log";\nimport { authenticatePolytaoUi } from "./polytao";')
  replace('      if (!authenticated(request.headers.get("Authorization")))',
    `      const modelAuthenticated = authenticated(request.headers.get("Authorization"));
      const uiAuthenticated = !modelAuthenticated && await authenticatePolytaoUi(request.headers.get("Authorization"), env);
      if (!modelAuthenticated && !uiAuthenticated)`)
  replace('      return response = await handleMcpRequest(request);',
    '      return response = await handleMcpRequest(request, uiAuthenticated ? "ui" : "model");')
  return source
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const target = process.argv[2]
  if (!target) throw new Error("Expected gateway MCP source path")
  await writeFile(target, patchGateway(await readFile(target, "utf8")))
  const serverTarget = process.argv[3]
  if (serverTarget) await writeFile(serverTarget, patchGatewayServer(await readFile(serverTarget, "utf8")))
}
