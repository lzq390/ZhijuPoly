import assert from "node:assert/strict";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const APP_SOURCE = "src/mountApp.tsx";
const CANVAS_SOURCE = "src/components/StructureWorkbenchPage.tsx";
const HOME_TITLE = "智聚万物智能体工作台";

/** Verify the actual lazy entry graph, without searching unrelated image files. */
export function verifyFrontendImageAssets(directory, expectedWorkspaceUrl = "") {
  const root = realpathSync(directory);
  const read = (file) => {
    assert.equal(typeof file, "string", "Asset path must be a string");
    assert.ok(file.length > 0 && !isAbsolute(file), `Invalid asset path: ${file}`);
    const path = realpathSync(resolve(root, file));
    const fromRoot = relative(root, path);
    assert.ok(fromRoot !== ".." && !fromRoot.startsWith(`..${sep}`) && !isAbsolute(fromRoot), `Asset escapes build directory: ${file}`);
    assert.ok(statSync(path).isFile(), `Asset is not a file: ${file}`);
    const text = readFileSync(path, "utf8");
    assert.ok(text.length > 0, `Empty asset: ${file}`);
    return text;
  };
  const html = read("index.html");
  const manifest = JSON.parse(read(".vite/manifest.json"));
  const entry = manifest["index.html"];
  assert.ok(entry?.isEntry && entry.file?.endsWith(".js"), "Missing HTML entry in Vite manifest");
  const scripts = [...html.matchAll(/<script\b([^>]*)>/gi)];
  assert.ok(scripts.some(([, attributes]) => {
    const source = attributes.match(/\bsrc\s*=\s*(["'])(.*?)\1/i)?.[2];
    return /\btype\s*=\s*(["'])module\1/i.test(attributes)
      && [entry.file, `/${entry.file}`, `./${entry.file}`].includes(source);
  }), "HTML does not load the manifest entry");

  const visit = (start, includeDynamic) => {
    const seen = new Set();
    const pending = [...start];
    while (pending.length) {
      const key = pending.pop();
      if (seen.has(key)) continue;
      const chunk = manifest[key];
      assert.ok(chunk && typeof chunk.file === "string", `Missing manifest chunk: ${key}`);
      seen.add(key);
      for (const field of includeDynamic ? ["imports", "dynamicImports"] : ["imports"]) {
        assert.ok(chunk[field] === undefined || Array.isArray(chunk[field]), `Invalid ${field}: ${key}`);
        pending.push(...(chunk[field] ?? []));
      }
    }
    return seen;
  };
  const reachable = visit(["index.html"], true);
  const contents = new Map();
  const filesFor = (keys) => [...new Set([...keys].flatMap(key => {
    const chunk = manifest[key];
    return [chunk.file, ...(chunk.css ?? []), ...(chunk.assets ?? [])];
  }))];
  // Missing lazy files must fail even when the server would return its SPA fallback.
  for (const file of filesFor(reachable)) contents.set(file, read(file));
  const locate = (source) => {
    const matches = [...reachable].filter(key => key === source || manifest[key].src === source);
    assert.equal(matches.length, 1, `Expected one reachable entry for ${source}`);
    return matches[0];
  };
  const app = visit([locate(APP_SOURCE)], false);
  const canvas = visit([locate(CANVAS_SOURCE)], false);
  const jsFor = (keys) => filesFor(keys).filter(file => file.endsWith(".js"));
  // AgentWorkspaceHomePage is statically imported by App. Its title and status
  // identify the chunk containing the actual workspace URL consumer, even if
  // Rollup extracts a shared chunk. Other lazy pages cannot satisfy this check.
  const homeChunks = jsFor(app).filter(file => contents.get(file).includes(HOME_TITLE));
  assert.equal(homeChunks.length, 1, "Expected one home workspace chunk in the App static imports");
  const home = contents.get(homeChunks[0]);
  assert.ok(home.includes("正在同步"), "Home workspace synchronization state is missing");
  if (expectedWorkspaceUrl) {
    assert.ok(home.includes(JSON.stringify(expectedWorkspaceUrl)) || home.includes(`'${expectedWorkspaceUrl}'`),
      `Home workspace chunk does not contain the configured URL: ${homeChunks[0]}`);
  } else {
    assert.ok(!/(?:127\.0\.0\.1|localhost):(4454|9011)\b/.test(home),
      `Unconfigured home workspace contains an active loopback URL: ${homeChunks[0]}`);
  }
  assert.ok(jsFor(canvas).some(file => contents.get(file).includes("/vendor/3Dmol-min.js")),
    "StructureWorkbenchPage static imports do not contain the 3Dmol loader");

  return {
    entry: entry.file,
    home: homeChunks[0],
    checkedAssets: filesFor(new Set([...visit(["index.html"], false), ...app, ...canvas]))
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    assert.ok(process.argv[2], "Usage: verify_frontend_image_assets.mjs <dist> [workspace-url]");
    console.log(JSON.stringify(verifyFrontendImageAssets(process.argv[2], process.argv[3] ?? ""), null, 2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
