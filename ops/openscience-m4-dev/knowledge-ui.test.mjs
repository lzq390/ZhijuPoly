import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import { installKnowledgeSearch } from "./knowledge-ui.mjs";

const { JSDOM } = createRequire(new URL("../../frontend/package.json", import.meta.url))("jsdom");
const panel = '<section aria-label="知识库管理"><div></div></section>';
const kb = "11111111-1111-4111-8111-111111111111";
const doc = "22222222-2222-4222-8222-222222222222";
const chunk = "33333333-3333-4333-8333-333333333333";
const path = `/knowledge-api/kbs/${kb}/documents/${doc}/chunks/${chunk}`;
const hit = { kb_id: kb, document_id: doc, chunk_id: chunk, citation_url: path,
  document_name: "<img src=x>", text: "<b>private record</b>" };
const json = value => ({ ok: true, json: async () => value });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

test("search and citation use authenticated fetch and render literal document text", async () => {
  const { window } = new JSDOM(panel, { url: "https://agent.m2.test/" });
  const { document } = window;
  const requests = [];
  const dispose = installKnowledgeSearch(document, async (url, options) => {
    requests.push({ url, options });
    return json(url === path ? hit : { results: [hit] });
  });
  try {
    document.querySelector("input").value = "private record";
    document.querySelector("form").dispatchEvent(new window.Event("submit", { cancelable: true }));
    await tick();
    assert.equal(requests[0].url, "/knowledge-api/search");
    assert.deepEqual(JSON.parse(requests[0].options.body), { query: "private record", top_k: 5 });
    const link = document.querySelector("li a");
    assert.equal(link.getAttribute("href"), path);
    assert.equal(document.querySelector("li h3").textContent, hit.document_name);
    link.click();
    await tick();
    assert.equal(requests[1].url, path);
    assert.equal(document.querySelector("blockquote").textContent, hit.text);
    assert.equal(document.querySelector("img,li b"), null);
  } finally { dispose(); window.close(); }
});

test("rejects a mismatched citation and discards pending results after a panel switch", async () => {
  const { window } = new JSDOM(panel);
  const { document } = window;
  let settle;
  const dispose = installKnowledgeSearch(document, () => new Promise(resolve => { settle = resolve; }));
  function submit() {
    document.querySelector("input").value = "term";
    document.querySelector("form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  }
  try {
    submit();
    settle(json({ results: [{ ...hit, citation_url: "https://external.invalid/private" }] }));
    await tick();
    assert.equal(document.querySelector("li a"), null);
    assert.equal(document.querySelector('[role="status"]').textContent, "检索失败，请重试。");
    submit();
    document.body.replaceChildren();
    await tick();
    document.body.innerHTML = panel;
    await tick();
    settle(json({ results: [hit] }));
    await tick();
    assert.equal(document.querySelector("li"), null);
    assert.equal(document.querySelector('[role="status"]').textContent, "");
    assert.equal(document.querySelectorAll('section[aria-label="私有文档全文检索"]').length, 1);
  } finally { dispose(); window.close(); }
});
