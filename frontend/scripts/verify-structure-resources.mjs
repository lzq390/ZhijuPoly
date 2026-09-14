// Validate the files that the currently shipped editor actually loads.
import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { resolve } from "node:path";
const root = resolve(process.argv[2] || "dist");
const iframeRoot = resolve(root, "ketcher");
const [host, engine] = await Promise.all([
  readFile(resolve(root, "index.html"), "utf8"),
  readFile(resolve(root, "structure-editor.json"), "utf8").then(JSON.parse)
]);
const assetPaths = html => [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
  .map(match => match[1]).filter(path => !/^(https?:|data:|#)/.test(path));
const files = new Set([
  ...assetPaths(host).map(path => resolve(root, path.replace(/^\//, ""))),
  ...engine.assets.map(asset => resolve(root, asset.path)),
  resolve(root, "vendor/3Dmol-min.js")
]);
assert.ok(["react", "iframe"].includes(engine.engine));
if (engine.engine === "iframe") {
  const [iframe, manifest] = await Promise.all([
    readFile(resolve(iframeRoot, "index.html"), "utf8"),
    readFile(resolve(iframeRoot, "asset-manifest.json"), "utf8").then(JSON.parse)
  ]);
  assert.match(iframe, /<title>Ketcher v3\.7\.0<\/title>/);
  assert.deepEqual(engine.nativeAssets, []);
  for (const path of [...assetPaths(iframe), ...Object.values(manifest.files)]) files.add(resolve(iframeRoot, path));
} else {
  assert.equal(engine.sdk, "3.8.0");
  assert.ok(engine.nativeAssets.some(path => /\.js$/.test(path)));
  assert.ok(engine.nativeAssets.some(path => /\.css$/.test(path)));
  const nativeCss = await Promise.all(engine.nativeAssets.filter(path => /\.css$/.test(path)).map(path => readFile(resolve(root, path), "utf8")));
  assert.ok(nativeCss.some(css => css.includes('data-editor-engine')));
  const entry = await readFile(resolve(root, assetPaths(host).find(path => /\.js$/.test(path)).replace(/^\//, "")), "utf8");
  assert.ok(!entry.includes('ketcher/index.html'), "Native entry must not reference the legacy application.");
}
await Promise.all([...files].map(async file => {
  assert.ok(file.startsWith(root + "/"), `Asset escapes build directory: ${file}`);
  const info = await stat(file);
  assert.ok(info.isFile() && info.size > 0, `Missing or empty asset: ${file}`);
}));
console.log(JSON.stringify({ engine: engine.engine, version: engine.sdk, files: files.size, nativeAssets: engine.nativeAssets, passed: true }));
