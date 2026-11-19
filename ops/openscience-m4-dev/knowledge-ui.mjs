// Loaded only by the independent M4 UI. Browser fetch supplies the current page's login.
const uuid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;

function citationPath(hit) {
  const { kb_id, document_id, chunk_id } = hit;
  if (![kb_id, document_id, chunk_id].every(id => typeof id === "string" && uuid.test(id))) {
    throw Error("Invalid knowledge citation");
  }
  const path = `/knowledge-api/kbs/${kb_id}/documents/${document_id}/chunks/${chunk_id}`;
  if (hit.citation_url !== path || typeof hit.document_name !== "string" || typeof hit.text !== "string") {
    throw Error("Invalid knowledge citation");
  }
  return path;
}

function mount(panel, fetchService) {
  const doc = panel.ownerDocument;
  function element(tag, text, classes) {
    const el = doc.createElement(tag);
    if (text) el.textContent = text;
    if (classes) el.className = classes;
    return el;
  }
  const section = element("section", "", "bg-background-base border border-border-base rounded-lg p-5 shadow-sm");
  section.setAttribute("aria-label", "私有文档全文检索");
  const heading = element("h2", "私有文档全文检索", "text-16-medium text-text-strong mb-4");
  const form = element("form", "", "flex flex-wrap items-end gap-3");
  const label = element("label", "检索我的私有文档", "flex flex-col gap-2 flex-1 text-13-medium text-text-base");
  const input = element("input", "", "border border-border-base rounded-md px-3 py-2 bg-background-base text-14-regular");
  input.type = "search";
  input.required = true;
  input.maxLength = 1000;
  const button = element("button", "检索正文", "px-3 py-2 rounded-md bg-accent-base text-white text-14-medium");
  button.type = "submit";
  label.append(input);
  form.append(label, button);
  const status = element("p", "", "text-13-regular text-text-base mt-3");
  status.setAttribute("role", "status");
  const list = element("ul", "", "flex flex-col gap-3 mt-3");
  list.setAttribute("aria-label", "全文检索结果");
  section.append(heading, form, status, list);
  (panel.firstElementChild || panel).append(section);

  let pending;
  function result(hit, signal) {
    const path = citationPath(hit);
    const li = element("li", "", "border border-border-base rounded-lg p-3");
    const title = element("h3", hit.document_name, "text-14-medium text-text-strong");
    const excerpt = element("p", hit.text, "text-14-regular text-text-base mb-2");
    const link = element("a", "打开原文引用", "text-13-medium text-accent-base");
    link.href = path;
    const source = element("section", "", "mt-3");
    source.setAttribute("aria-label", "引用原文");
    source.hidden = true;
    let reading = false;
    link.addEventListener("click", async event => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (reading || signal.aborted) return;
      reading = true;
      source.hidden = false;
      source.textContent = "正在读取原文…";
      try {
        const response = await fetchService(path, { signal });
        if (!response.ok) throw Error("Unavailable knowledge citation");
        const value = await response.json();
        if (citationPath(value) !== path) throw Error("Unexpected knowledge citation");
        if (signal.aborted) return;
        const name = element("h4", value.document_name, "text-14-medium text-text-strong");
        const reference = element("p", "片段 " + value.chunk_id, "text-12-regular text-text-weak");
        const quote = element("blockquote", value.text, "text-14-regular text-text-base");
        quote.style.whiteSpace = "pre-wrap";
        source.replaceChildren(name, reference, quote);
      } catch {
        if (!signal.aborted) source.textContent = "无法读取这条引用，请重新检索。";
      } finally {
        reading = false;
      }
    });
    li.append(title, excerpt, link, source);
    return li;
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    pending?.abort();
    pending = new AbortController();
    const { signal } = pending;
    list.replaceChildren();
    const query = input.value.trim();
    if (!query || new TextEncoder().encode(query).length > 1000) {
      status.textContent = "请输入 1 至 1000 个 UTF-8 字节的检索词。";
      return;
    }
    status.textContent = "正在检索…";
    try {
      const response = await fetchService("/knowledge-api/search", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, top_k: 5 }), signal,
      });
      if (!response.ok) throw Error("Knowledge search unavailable");
      const { results } = await response.json();
      if (!Array.isArray(results)) throw Error("Invalid search results");
      if (signal.aborted) return;
      list.replaceChildren(...results.map(hit => result(hit, signal)));
      status.textContent = results.length ? `找到 ${results.length} 条正文片段。` : "未找到匹配的私有文档。";
    } catch {
      if (!signal.aborted) status.textContent = "检索失败，请重试。";
    }
  });
  return { section, remove() { pending?.abort(); section.remove(); } };
}

export function installKnowledgeSearch(doc, fetchService) {
  let mounted;
  function refresh() {
    if (mounted?.section.isConnected) return;
    mounted?.remove();
    mounted = undefined;
    const panel = doc.querySelector('section[aria-label="知识库管理"]');
    if (panel) mounted = mount(panel, fetchService);
  }
  refresh();
  const observer = new doc.defaultView.MutationObserver(refresh);
  observer.observe(doc.body, { subtree: true, childList: true });
  return () => { observer.disconnect(); mounted?.remove(); };
}

if (typeof document !== "undefined") installKnowledgeSearch(document, (...args) => window.fetch(...args));
