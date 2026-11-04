import assert from "node:assert/strict";

const base = process.argv[2];
if (!base) throw new Error("usage: node verify-cache.mjs BASE_URL [RETAINED_ASSET_PATH ...]");

async function request(path) {
  const response = await fetch(new URL(path, base), { redirect: "manual", signal: AbortSignal.timeout(10_000) });
  return {
    path,
    status: response.status,
    cacheControl: response.headers.get("cache-control"),
    contentType: response.headers.get("content-type"),
    body: await response.text()
  };
}

function cacheTokens(response, expected) {
  assert.deepEqual(response.cacheControl?.split(",").map(part => part.trim()).sort(), [...expected].sort(), response.path);
}

const root = await request("/");
assert.equal(root.status, 200, "/");
const script = [...root.body.matchAll(/<script\b[^>]*>/g)].map(match => match[0]).find(tag => /type=["']module["']/.test(tag));
const entry = script?.match(/src=["']([^"']+)["']/)?.[1];
assert.ok(entry?.startsWith("/assets/"), "root HTML must reference an absolute local module entry");

const checks = [];
for (const path of ["/", "/index.html", "/nexpoly-cache-verification/spa", "/openscience-theme-preload.js", "/atlas-favicon.svg"]) {
  const response = path === "/" ? root : await request(path);
  assert.equal(response.status, 200, path);
  cacheTokens(response, ["no-cache", "must-revalidate"]);
  checks.push({ path, status: response.status, cacheControl: response.cacheControl });
}
for (const path of new Set([entry, ...process.argv.slice(3)])) {
  assert.ok(path.startsWith("/assets/") && !path.includes(".."), "retained assets must be local /assets/ paths");
  const response = await request(path);
  assert.equal(response.status, 200, path);
  cacheTokens(response, ["public", "max-age=31536000", "immutable"]);
  checks.push({ path, status: response.status, cacheControl: response.cacheControl });
}
const missing = await request("/assets/nexpoly-cache-verification-missing.js");
assert.equal(missing.status, 404, missing.path);
cacheTokens(missing, ["no-store"]);
checks.push({ path: missing.path, status: missing.status, cacheControl: missing.cacheControl });
process.stdout.write(`${JSON.stringify({ passed: true, checks })}\n`);
