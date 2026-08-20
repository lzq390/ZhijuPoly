#!/usr/bin/env node

import assert from "node:assert/strict"
import test from "node:test"
import vm from "node:vm"

import {
  BASE_BUNDLE_SHA256,
  PATCHED_STATIC_TREE_SHA256,
  NEW_RESOLVER,
  OLD_CALL,
  OLD_RESOLVER,
  TRUSTED_PARENT_ORIGINS,
  TRUSTED_PARENT_POLICY_SHA256,
  patchBundle,
  rewriteIndex,
} from "./patch.mjs"

function resolveParent(referrer, embedded = true) {
  const resolver = vm.runInNewContext(
    `(() => {
      function ZM(value) {
        const normalized = value?.trim()
        if (!normalized || normalized === "*") return undefined
        try {
          const url = new URL(normalized)
          if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password) {
            return undefined
          }
          return url.origin
        } catch {
          return undefined
        }
      }
      return ${NEW_RESOLVER}
    })()`,
    { document: { referrer }, URL },
  )
  return resolver("ignored-legacy-config", embedded)
}

test("the governed constants pin the deployed bridge contract", () => {
  assert.match(BASE_BUNDLE_SHA256, /^[0-9a-f]{64}$/)
  assert.equal(
    PATCHED_STATIC_TREE_SHA256,
    "3810ec7d6428a960c14b305d5925a22dd03769c9ab36c091a7a387b7b82e3969",
  )
  assert.equal(
    TRUSTED_PARENT_POLICY_SHA256,
    "955ae6f5f3d0710dcaacc0906f6326a4ba99321a0e47fc928c198c8967dd0042",
  )
  assert.deepEqual(TRUSTED_PARENT_ORIGINS, [
    "http://114.214.255.154:9000",
    "http://114.214.255.154:9001",
  ])
  assert.match(NEW_RESOLVER, /document\.referrer/)
  assert.doesNotMatch(NEW_RESOLVER, /postMessage\([^)]*,\s*["']\*["']/)
  assert.equal(NEW_RESOLVER.includes('"http://114.214.255.154:9000"'), true)
  assert.equal(NEW_RESOLVER.includes('"http://114.214.255.154:9001"'), true)
})

test("the resolver accepts only the two reviewed parent origins", () => {
  assert.equal(resolveParent("http://114.214.255.154:9000/workspace"), "http://114.214.255.154:9000")
  assert.equal(resolveParent("http://114.214.255.154:9001/"), "http://114.214.255.154:9001")
  assert.equal(resolveParent("http://114.214.255.154:9002/"), undefined)
  assert.equal(resolveParent(""), undefined)
  assert.equal(resolveParent("http://user:secret@114.214.255.154:9000/"), undefined)
  assert.equal(resolveParent("http://114.214.255.154:9000/", false), undefined)
})

test("the exact deployed bundle is the only accepted patch input", () => {
  assert.throws(() => patchBundle(`${OLD_RESOLVER}${OLD_CALL}${OLD_CALL}`), /SHA-256 differs/)
})

test("index rewrite requires the governed base index", () => {
  assert.throws(
    () => rewriteIndex('<script src="/assets/index-B2eNxQLj.js"></script>', "index-nexpoly-0123456789ab.js"),
    /SHA-256 differs/,
  )
})

test("patched bundle names are content-addressed", () => {
  assert.throws(() => rewriteIndex("", "../escape.js"), /SHA-256 differs/)
})
