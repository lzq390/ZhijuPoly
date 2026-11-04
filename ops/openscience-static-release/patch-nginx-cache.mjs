import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const marker = "# nexpoly static cache policy v1";
const missingLocation = "@nexpoly_static_not_found";

function block(config, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...config.matchAll(new RegExp(`(^[ \\t]*)location\\s+${escaped}\\s*\\{([^{}]*)\\}`, "gm"))];
  if (matches.length !== 1) throw new Error(`expected exactly one simple location ${selector}`);
  return { text: matches[0][0], indent: matches[0][1], body: matches[0][2] };
}

function statements(body) {
  return body.replace(/#[^\n]*/g, "").split(";").map(part => part.trim().replace(/\s+/g, " ")).filter(Boolean);
}

function requireBody(location, expected, label) {
  if (JSON.stringify(statements(location.body)) !== JSON.stringify(expected)) {
    throw new Error(`unsupported ${label} location directives; review the upstream nginx configuration`);
  }
}

const assetPolicy = [
  `try_files $uri ${missingLocation}`,
  "expires off",
  'add_header Cache-Control "public, max-age=31536000, immutable"'
];
const htmlPolicy = ["try_files $uri =404", "expires off", 'add_header Cache-Control "no-cache, must-revalidate" always'];
const fallbackPolicy = ["try_files $uri $uri/ /index.html", "expires off", 'add_header Cache-Control "no-cache, must-revalidate" always'];
const missingPolicy = ["expires off", 'add_header Cache-Control "no-store" always', "return 404"];

function render(indent, selector, directives) {
  return `${indent}location ${selector} {\n${directives.map(value => `${indent}  ${value};`).join("\n")}\n${indent}}`;
}

/** Change static cache handling while leaving upstream/API/bridge routing intact. */
export function patchNginxCache(config) {
  if ((config.match(/^\s*server\s*\{/gm) ?? []).length !== 1) {
    throw new Error("expected exactly one nginx server block");
  }
  const assets = block(config, "^~ /assets/");
  const fallback = block(config, "/");
  if (config.includes(marker)) {
    requireBody(assets, assetPolicy, "assets");
    requireBody(fallback, fallbackPolicy, "fallback");
    requireBody(block(config, "= /index.html"), htmlPolicy, "HTML");
    requireBody(block(config, missingLocation), missingPolicy, "missing asset");
    return config;
  }
  if (config.includes(missingLocation) || /location\s+=\s+\/index\.html\s*\{/.test(config)) {
    throw new Error("existing HTML or named static location requires manual review");
  }
  requireBody(assets, ["try_files $uri =404", "expires 1y", "add_header Cache-Control 'public, immutable'"], "assets");
  requireBody(fallback, ["try_files $uri $uri/ /index.html"], "fallback");
  const replacement = [
    `${assets.indent}${marker}`,
    render(assets.indent, "= /index.html", htmlPolicy),
    render(assets.indent, "^~ /assets/", assetPolicy),
    render(assets.indent, missingLocation, missingPolicy)
  ].join("\n");
  return config.replace(assets.text, replacement).replace(fallback.text, render(fallback.indent, "/", fallbackPolicy));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 4) throw new Error("usage: node patch-nginx-cache.mjs INPUT_NGINX_CONF NEW_OUTPUT_NGINX_CONF");
    const result = patchNginxCache(readFileSync(process.argv[2], "utf8"));
    writeFileSync(process.argv[3], result, { flag: "wx", mode: 0o644 });
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
