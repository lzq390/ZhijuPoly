import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

export function structureEngine(value: string | undefined) {
  const engine = value ?? "react";
  if (engine !== "react" && engine !== "iframe") throw new Error(`Invalid VITE_STRUCTURE_EDITOR_ENGINE: ${engine}; expected react|iframe`);
  const implementation = fileURLToPath(new URL(`../src/components/structure-workbench/${engine === "react" ? "React" : "Iframe"}StructureEditor.tsx`, import.meta.url));
  const preload = fileURLToPath(new URL(`../src/components/structure-workbench/preload${engine === "react" ? "React" : "Iframe"}StructureEditor.ts`, import.meta.url));
  const metadata: Plugin = {
    name: "structure-engine-manifest",
    // Vite removes CSS-only JavaScript chunks during generateBundle. Describe
    // the final delivery set so lazy page styles never create phantom assets.
    generateBundle: { order: "post", handler(_options, bundle) {
      const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
      const patches = JSON.parse(readFileSync(new URL("../patches/ketcher-manifest.json", import.meta.url), "utf8"));
      const nativeAssets = Object.values(bundle).filter(output => output.type === "chunk"
        ? Object.keys(output.modules).some(id => /node_modules\/ketcher-|KetcherReactRuntime/.test(id))
        : /^assets\/KetcherReactRuntime-.*\.css$/.test(output.fileName)).map(output => output.fileName);
      if (engine === "iframe" && nativeAssets.length) throw new Error("Iframe build unexpectedly contains native Ketcher modules.");
      if (engine === "react" && !nativeAssets.length) throw new Error("React build is missing native Ketcher modules.");
      this.emitFile({ type: "asset", fileName: "structure-editor.json", source: JSON.stringify({
        engine, sdk: engine === "react" ? "3.8.0" : "3.7.0", patch: patches.revision,
        patchHashes: patches.patches,
        lockSha256: hash(readFileSync(new URL("../package-lock.json", import.meta.url))),
        nativeAssets, assets: Object.values(bundle).map(output => ({ path: output.fileName,
          bytes: Buffer.byteLength(output.type === "chunk" ? output.code : output.source) }))
      }, null, 2) });
    } }
  };
  return { engine, implementation, preload, metadata };
}
