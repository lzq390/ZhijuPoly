#!/usr/bin/env node

import { createHash } from "node:crypto"
import { mkdir, readdir, readFile, rm, stat, writeFile } from "node:fs/promises"
import { basename, join, relative, resolve } from "node:path"
import { fileURLToPath } from "node:url"

export const BASE_INDEX_RELATIVE_PATH = "index.html"
export const BASE_BUNDLE_RELATIVE_PATH = "assets/index-B2eNxQLj.js"
export const BASE_INDEX_SHA256 = "30c4ff024456fba5ad397fe6d17ede57427c6c7fe189e27d5488c7009452c9ff"
export const BASE_BUNDLE_SHA256 = "6b4c7264c00e1c83a6d8fe610b2da4fe4c61c86d7a2626e2cdc2f4a1849f3cf2"
export const BASE_STATIC_TREE_SHA256 = "b942ea5aac61ed7568327e40a3193b3c35c12380f05234775f122b6a2b98bc9d"
export const PATCHED_STATIC_TREE_SHA256 = "32f45b16e585ef348b4a83a9763412476568ec1781aecb5be69ebd7d7f3c54fd"

export const OLD_RESOLVER = "function mf(e,t){if(t)return ZM(e)}"
export const OLD_CALL = 'mf("http://114.214.255.154:9001",i!==window)'
export const TRUSTED_PARENT_ORIGINS = [
  "http://114.214.255.154:9000",
  "http://114.214.255.154:9001",
]
export const TRUSTED_PARENT_POLICY_SHA256 = "955ae6f5f3d0710dcaacc0906f6326a4ba99321a0e47fc928c198c8967dd0042"
export const NEW_RESOLVER =
  "function mf(e,t){if(!t)return;const n=ZM(document.referrer);return n&&" +
  JSON.stringify(TRUSTED_PARENT_ORIGINS) +
  ".includes(n)?n:void 0}"

function sha256(value) {
  return createHash("sha256").update(value).digest("hex")
}

function countOccurrences(value, needle) {
  if (!needle) throw new Error("needle must not be empty")
  let count = 0
  let cursor = 0
  while (true) {
    const found = value.indexOf(needle, cursor)
    if (found < 0) return count
    count += 1
    cursor = found + needle.length
  }
}

function replaceExactly(value, needle, replacement, expectedCount, label) {
  const count = countOccurrences(value, needle)
  if (count !== expectedCount) {
    throw new Error(`${label} count differs: expected ${expectedCount}, found ${count}`)
  }
  return value.split(needle).join(replacement)
}

export function patchBundle(source) {
  if (sha256(source) !== BASE_BUNDLE_SHA256) {
    throw new Error("OpenScience base bundle SHA-256 differs")
  }
  if (countOccurrences(source, OLD_CALL) !== 2) {
    throw new Error("OpenScience trusted-parent call count differs")
  }
  const patched = replaceExactly(source, OLD_RESOLVER, NEW_RESOLVER, 1, "bridge resolver")
  if (countOccurrences(patched, OLD_RESOLVER) !== 0) {
    throw new Error("old bridge resolver remains after patch")
  }
  if (countOccurrences(patched, NEW_RESOLVER) !== 1) {
    throw new Error("new bridge resolver was not installed exactly once")
  }
  if (countOccurrences(patched, OLD_CALL) !== 2) {
    throw new Error("bridge callers changed unexpectedly")
  }
  return patched
}

export function rewriteIndex(source, newBundleName) {
  if (sha256(source) !== BASE_INDEX_SHA256) {
    throw new Error("OpenScience base index SHA-256 differs")
  }
  if (basename(newBundleName) !== newBundleName || !/^index-nexpoly-[0-9a-f]{12}\.js$/.test(newBundleName)) {
    throw new Error("patched bundle name is invalid")
  }
  const oldReference = `/${BASE_BUNDLE_RELATIVE_PATH}`
  const newReference = `/assets/${newBundleName}`
  const oldEntry = `<script type="module" crossorigin src="${oldReference}"></script>`
  const importMap =
    `<script type="importmap">` +
    JSON.stringify({ imports: { [oldReference]: newReference } }) +
    `</script>`
  const newEntry = `${importMap}\n    <script type="module" crossorigin src="${newReference}"></script>`
  if (countOccurrences(source, oldReference) !== 1) {
    throw new Error("base index bundle reference count differs")
  }
  if (countOccurrences(source, '<script type="importmap">') !== 0) {
    throw new Error("base index unexpectedly contains an import map")
  }
  const rewritten = replaceExactly(source, oldEntry, newEntry, 1, "index module entry")
  if (
    countOccurrences(rewritten, oldReference) !== 1 ||
    countOccurrences(rewritten, newReference) !== 2 ||
    countOccurrences(rewritten, importMap) !== 1 ||
    rewritten.indexOf(importMap) > rewritten.indexOf(newEntry.split("\n")[1])
  ) {
    throw new Error("index bundle reference rewrite is not exact")
  }
  return rewritten
}

async function regularFiles(root, directory = root) {
  const entries = await readdir(directory, { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      files.push(...(await regularFiles(root, path)))
      continue
    }
    if (!entry.isFile()) {
      throw new Error(`OpenScience static tree contains a non-regular path: ${relative(root, path)}`)
    }
    files.push(path)
  }
  return files
}

export async function staticTreeSha256(root) {
  const lines = []
  const files = await regularFiles(root)
  files.sort((left, right) =>
    Buffer.compare(Buffer.from(relative(root, left)), Buffer.from(relative(root, right))),
  )
  for (const path of files) {
    const contents = await readFile(path)
    lines.push(`${sha256(contents)}  ./${relative(root, path)}\n`)
  }
  return sha256(lines.join(""))
}

export async function patchStaticTree(sourceRoot, outputRoot) {
  const resolvedSource = resolve(sourceRoot)
  const resolvedOutput = resolve(outputRoot)
  if (resolvedSource === resolvedOutput) throw new Error("source and output roots must differ")
  const actualParentPolicySha256 = sha256(`${TRUSTED_PARENT_ORIGINS.join("\n")}\n`)
  if (actualParentPolicySha256 !== TRUSTED_PARENT_POLICY_SHA256) {
    throw new Error("OpenScience trusted-parent policy SHA-256 differs")
  }
  const actualStaticTreeSha256 = await staticTreeSha256(resolvedSource)
  if (actualStaticTreeSha256 !== BASE_STATIC_TREE_SHA256) {
    throw new Error(
      `OpenScience base static-tree SHA-256 differs: expected ${BASE_STATIC_TREE_SHA256}, found ${actualStaticTreeSha256}`,
    )
  }

  const indexPath = join(resolvedSource, BASE_INDEX_RELATIVE_PATH)
  const bundlePath = join(resolvedSource, BASE_BUNDLE_RELATIVE_PATH)
  const index = await readFile(indexPath, "utf8")
  const bundle = await readFile(bundlePath, "utf8")
  const patchedBundle = patchBundle(bundle)
  const patchedBundleSha256 = sha256(patchedBundle)
  const patchedBundleName = `index-nexpoly-${patchedBundleSha256.slice(0, 12)}.js`
  const patchedIndex = rewriteIndex(index, patchedBundleName)

  await rm(resolvedOutput, { recursive: true, force: true })
  await mkdir(join(resolvedOutput, "assets"), { recursive: true, mode: 0o755 })
  await writeFile(join(resolvedOutput, "index.html"), patchedIndex, { mode: 0o644 })
  await writeFile(join(resolvedOutput, "assets", patchedBundleName), patchedBundle, { mode: 0o644 })

  const indexStat = await stat(join(resolvedOutput, "index.html"))
  const bundleStat = await stat(join(resolvedOutput, "assets", patchedBundleName))
  if ((indexStat.mode & 0o777) !== 0o644 || (bundleStat.mode & 0o777) !== 0o644) {
    throw new Error("patched static files are not mode 0644")
  }

  return {
    baseBundleSha256: BASE_BUNDLE_SHA256,
    baseIndexSha256: BASE_INDEX_SHA256,
    baseStaticTreeSha256: BASE_STATIC_TREE_SHA256,
    patchedStaticTreeSha256: PATCHED_STATIC_TREE_SHA256,
    patchedBundleName,
    patchedBundleSha256,
    patchedIndexSha256: sha256(patchedIndex),
    trustedParentOrigins: TRUSTED_PARENT_ORIGINS,
    trustedParentPolicySha256: TRUSTED_PARENT_POLICY_SHA256,
  }
}

async function main() {
  const [sourceRoot, outputRoot] = process.argv.slice(2)
  if (!sourceRoot || !outputRoot) {
    throw new Error("usage: patch.mjs <source-static-root> <output-root>")
  }
  const result = await patchStaticTree(sourceRoot, outputRoot)
  process.stdout.write(`${JSON.stringify(result)}\n`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
    process.exitCode = 1
  })
}
