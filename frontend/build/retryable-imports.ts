import type { Plugin } from "vite";

/** Direct URL retries must expose the same namespace as the original import.
 * Without a strict facade, Rollup can rewrite an import to sharedChunk.namespace
 * while a browser retry of that URL would return the whole shared chunk.
 */
export function retryableImports(): Plugin {
  return {
    name: "retryable-import-entries",
    apply: "build",
    moduleParsed(module) {
      if (!/\/src\/(?:pages\.ts|components\/structure-workbench\/loadKetcherRuntime\.ts)$/.test(module.id)) return;
      for (const id of module.dynamicallyImportedIds) {
        this.emitFile({ type: "chunk", id, preserveSignature: "strict" });
      }
    }
  };
}
