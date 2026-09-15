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
      const runtimeAsset = bundle['ketcher-runtime.json'];
      const runtime = runtimeAsset?.type === 'asset' ? JSON.parse(String(runtimeAsset.source)) : undefined;
      const nativeAssets: string[] = runtime ? runtime.assets.map(asset => `assets/ketcher/${runtime.version}/${asset.file}`) : [];
      const inlineSdk = Object.values(bundle).filter(output => output.type === 'chunk' &&
        Object.keys(output.modules).some(id => /node_modules\/ketcher-/.test(id)));
      if (inlineSdk.length) throw new Error('Ketcher SDK unexpectedly entered the host bundle.');
      for (const asset of runtime?.assets || []) {
        const output = bundle[`assets/ketcher/${runtime.version}/${asset.file}`];
        if (!output || output.type !== 'asset' || hash(Buffer.from(output.source)) !== asset.sha256) {
          throw new Error(`Missing or corrupt native SDK artifact: ${asset.file}`);
        }
      }
      if (engine === "iframe" && nativeAssets.length) throw new Error("Iframe build unexpectedly contains native Ketcher modules.");
      if (engine === "react" && !nativeAssets.length) throw new Error("React build is missing native Ketcher modules.");
      this.emitFile({ type: "asset", fileName: "structure-editor.json", source: JSON.stringify({
        engine, sdk: engine === "react" ? "3.8.0" : "3.7.0", patch: patches.revision,
        patchHashes: patches.patches,
        lockSha256: hash(readFileSync(new URL("../package-lock.json", import.meta.url))),
        nativeRuntime: runtime ? { version: runtime.version, patchRevision: runtime.patchRevision } : undefined,
        nativeAssets, assets: Object.values(bundle).map(output => ({ path: output.fileName,
          bytes: Buffer.byteLength(output.type === "chunk" ? output.code : output.source) }))
      }, null, 2) });
    } }
  };
  return { engine, implementation, preload, metadata };
}
