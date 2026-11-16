import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { pathToFileURL } from "node:url"

export const EXPECTED_MCP_SHA256 = "8e3d56f40cb0bb1f7234321d24a8d7ccee244ecda4fec50d20fa7c2a96beeeb4"

export function patchGateway(source) {
  if (createHash("sha256").update(source).digest("hex") !== EXPECTED_MCP_SHA256) {
    throw new Error("Capability gateway source differs from the reviewed base")
  }
  const replace = (before, after) => {
    if (source.split(before).length !== 2) throw new Error("Capability gateway patch anchor is not unique")
    source = source.replace(before, after)
  }
  replace('import { logEvent } from "./log";',
    'import { logEvent } from "./log";\nimport { propertyFilterProxy, isPropertyFilterTool } from "./property-filter";')
  replace('  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));',
    `  // nexpoly-filter-gateway-v1: preserve the six existing capabilities if the added upstream is unavailable.
  server.setRequestHandler(ListToolsRequestSchema, async () => {
    try {
      return { tools: [...TOOLS, ...await propertyFilterProxy.listTools()] };
    } catch {
      logEvent({ evt: "filter_registry_unavailable", error: "Property filtering is temporarily unavailable" });
      return { tools: TOOLS };
    }
  });`)
  replace('    try {\n      let result: unknown;',
    `    try {
      if (isPropertyFilterTool(params.name)) {
        const result = await propertyFilterProxy.callTool(params.name, params.arguments ?? {});
        ok = !result.isError;
        // Return the full MCP result, including structuredContent, attachments and metadata.
        return result;
      }
      let result: unknown;`)
  return source
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const target = process.argv[2]
  if (!target) throw new Error("Expected gateway MCP source path")
  await writeFile(target, patchGateway(await readFile(target, "utf8")))
}
