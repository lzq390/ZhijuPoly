import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { verifyFrontendImageAssets } from "./verify_frontend_image_assets.mjs";

const WORKSPACE_URL = "http://114.214.255.154:9011/";
const APP = "src/mountApp.tsx";
const CANVAS = "src/components/StructureWorkbenchPage.tsx";
const HOME = 'const title="智聚万物智能体工作台",status="正在同步";';

function fixture(t, { workspaceUrl = "", sharedHome = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), "frontend-image-assets-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const write = (file, contents) => {
    mkdirSync(dirname(join(root, file)), { recursive: true });
    writeFileSync(join(root, file), contents);
  };
  const manifest = {
    "index.html": { file: "assets/index.js", isEntry: true, dynamicImports: [APP, CANVAS, "unrelated.ts"] },
    [APP]: { file: "assets/app.js", src: APP, imports: sharedHome ? ["_home.js"] : [] },
    [CANVAS]: { file: "assets/canvas.js", src: CANVAS, imports: ["_preview.js"], css: ["assets/canvas.css"] },
    "_preview.js": { file: "assets/preview.js" },
    "unrelated.ts": { file: "assets/unrelated.js", src: "unrelated.ts" }
  };
  if (sharedHome) manifest["_home.js"] = { file: "assets/home.js" };
  const home = sharedHome ? "assets/home.js" : "assets/app.js";
  write("index.html", '<script type="module" crossorigin src="/assets/index.js"></script>');
  write("assets/index.js", 'import("./app.js");');
  write("assets/app.js", "export const mountApp=()=>{};");
  write(home, `${HOME}const workspace=${JSON.stringify(workspaceUrl)};`);
  write("assets/canvas.js", 'import "./preview.js";');
  write("assets/preview.js", 'const source="/vendor/3Dmol-min.js";');
  write("assets/canvas.css", ".canvas{}");
  write("assets/unrelated.js", `const staleConfiguration=${JSON.stringify(WORKSPACE_URL)};`);
  const saveManifest = () => write(".vite/manifest.json", JSON.stringify(manifest));
  saveManifest();
  return { root, write, manifest, saveManifest, home };
}

test("accepts lazy chunks when the HTML entry contains no feature markers", t => {
  const f = fixture(t);
  const result = verifyFrontendImageAssets(f.root);
  assert.equal(result.home, f.home);
  assert.ok(result.checkedAssets.includes("assets/preview.js"));
  assert.ok(result.checkedAssets.includes("assets/canvas.css"));
  assert.ok(!result.checkedAssets.includes("assets/unrelated.js"));
});

test("finds configured home in a shared static chunk", t => {
  const f = fixture(t, { workspaceUrl: WORKSPACE_URL, sharedHome: true });
  assert.equal(verifyFrontendImageAssets(f.root, WORKSPACE_URL).home, "assets/home.js");
});

test("correct URL in an unrelated lazy page cannot hide wrong home configuration", t => {
  const f = fixture(t, { workspaceUrl: "https://wrong.example/" });
  assert.throws(() => verifyFrontendImageAssets(f.root, WORKSPACE_URL), /configured URL/);
});

test("correct URL in another App dependency cannot hide wrong home configuration", t => {
  const f = fixture(t, { workspaceUrl: "https://wrong.example/", sharedHome: true });
  f.write("assets/app.js", `const staleConfiguration=${JSON.stringify(WORKSPACE_URL)};`);
  assert.throws(() => verifyFrontendImageAssets(f.root, WORKSPACE_URL), /configured URL/);
});

test("a URL prefix is not accepted as the exact configured URL", t => {
  const f = fixture(t, { workspaceUrl: `${WORKSPACE_URL}wrong-path` });
  assert.throws(() => verifyFrontendImageAssets(f.root, WORKSPACE_URL), /configured URL/);
});

test("rejects an active loopback URL in an unconfigured home", t => {
  const f = fixture(t, { workspaceUrl: "http://localhost:9011/" });
  assert.throws(() => verifyFrontendImageAssets(f.root), /active loopback/);
});

test("unreachable manifest entries cannot satisfy source checks", t => {
  const f = fixture(t);
  f.manifest["index.html"].dynamicImports = [CANVAS, "unrelated.ts"];
  f.saveManifest();
  assert.throws(() => verifyFrontendImageAssets(f.root), /reachable entry/);
});

test("3Dmol from another page cannot satisfy the canvas loader check", t => {
  const f = fixture(t);
  f.manifest[CANVAS].imports = [];
  f.write("assets/unrelated.js", 'const source="/vendor/3Dmol-min.js";');
  f.saveManifest();
  assert.throws(() => verifyFrontendImageAssets(f.root), /3Dmol loader/);
});

test("requires every reachable lazy file to exist", t => {
  const f = fixture(t);
  rmSync(join(f.root, "assets/unrelated.js"));
  assert.throws(() => verifyFrontendImageAssets(f.root), /ENOENT/);
});

test("rejects missing chunk references, CSS files, manifest and wrong HTML entry", async t => {
  for (const kind of ["reference", "css", "manifest", "html"]) {
    await t.test(kind, t => {
      const f = fixture(t);
      if (kind === "reference") {
        f.manifest[APP].imports = ["_missing.js"];
        f.saveManifest();
      } else if (kind === "html") {
        f.write("index.html", '<script type="module" src="/assets/obsolete.js"></script>');
      } else {
        rmSync(join(f.root, kind === "css" ? "assets/canvas.css" : ".vite/manifest.json"));
      }
      assert.throws(() => verifyFrontendImageAssets(f.root));
    });
  }
});
