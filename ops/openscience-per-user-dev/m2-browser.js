// Runs synchronously in the dedicated Agent origin before OpenScience starts.
(() => {
  const key = "np_page";
  const current = new URL(location.href);
  // The history entry belongs to this document. Never recover a different
  // document's context from a shared cookie or another tab's storage.
  const page = current.searchParams.get(key) || history.state?.npPage;
  const nativeFetch = window.fetch.bind(window);
  // An iframe refresh after an SPA transition may have an empty referrer.
  // Preserve the parent associated with this page, scoped to its history entry.
  const savedParent = page && history.state?.npPage === page && history.state.npParent;
  const parentOrigin = savedParent || (document.referrer ? new URL(document.referrer).origin : null);
  let expired = false;
  function expire() {
    if (expired) return;
    expired = true;
    document.documentElement.style.visibility = "hidden";
    if (parentOrigin && parent !== window) {
      parent.postMessage({ type: "nexpoly:openscience:expired", page_context: page }, parentOrigin);
    }
    // Replace the document and terminate the old app and its running listeners.
    location.replace("/_nexpoly/expired");
  }
  document.documentElement.style.visibility = "hidden";
  if (!page) { expire(); return; }
  const initialState = history.state;
  history.replaceState({ ...initialState, npPage: page, npParent: parentOrigin }, "");
  for (const method of ["pushState", "replaceState"]) {
    const original = history[method].bind(history);
    history[method] = (state, unused, url) => original({ ...state, npPage: page, npParent: parentOrigin }, unused, url);
  }
  // The pinned UI stores local recents, drafts and settings in Web Storage.
  // Give every loaded document its own ephemeral store, including old tabs.
  function privateStorage() {
    const values = new Map();
    return {
      get length() { return values.size; },
      key: index => Array.from(values.keys())[index] ?? null,
      getItem: key => values.get(String(key)) ?? null,
      setItem: (key, value) => { values.set(String(key), String(value)); },
      removeItem: key => { values.delete(String(key)); },
      clear: () => values.clear(),
    };
  }
  for (const name of ["localStorage", "sessionStorage"]) {
    Object.defineProperty(window, name, { value: privateStorage(), configurable: false });
  }

  function isPrivateUrl(url) {
    return url.origin === location.origin && /^\/(?:openscience-api|api|knowledge-api)(?:\/|$)/.test(url.pathname);
  }
  const privateUrl = input => new URL(input instanceof Request ? input.url : input, location.href);
  window.fetch = async (input, init) => {
    const url = privateUrl(input);
    if (!isPrivateUrl(url)) return nativeFetch(input, init);
    if (expired) throw new Error("Workspace authorization expired");
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set("X-Nexpoly-Page", page);
    const response = await nativeFetch(input, { ...init, headers, credentials: "same-origin", cache: "no-store" });
    if (response.status === 401) expire();
    return response;
  };

  const scopedUrl = input => {
    const url = privateUrl(input);
    if (isPrivateUrl(url)) url.searchParams.set(key, page);
    return url.href;
  };
  const nativeXhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    nativeXhrOpen.call(this, method, url, ...rest);
    if (isPrivateUrl(privateUrl(url))) this.setRequestHeader("X-Nexpoly-Page", page);
  };
  // Embedded attachment previews can use native image/media requests rather
  // than fetch. Scope only private API destinations before the request starts.
  for (const type of [HTMLImageElement, HTMLMediaElement, HTMLSourceElement]) {
    const descriptor = Object.getOwnPropertyDescriptor(type.prototype, "src");
    if (descriptor?.set) {
      Object.defineProperty(type.prototype, "src", {
        ...descriptor, set(value) { descriptor.set.call(this, scopedUrl(value)); },
      });
    }
  }
  const nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name, value) {
    if (name.toLowerCase() === "src" && ["IMG", "AUDIO", "VIDEO", "SOURCE"].includes(this.tagName)) {
      value = scopedUrl(value);
    }
    return nativeSetAttribute.call(this, name, value);
  };
  if (window.EventSource) {
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url, options) { super(scopedUrl(url), options); }
    };
  }
  // Downloads opened by the browser cannot attach fetch headers.
  document.addEventListener("click", event => {
    const link = event.target instanceof Element && event.target.closest("a[href]");
    if (link && isPrivateUrl(privateUrl(link.href))) link.href = scopedUrl(link.href);
  }, true);
  const open = window.open.bind(window);
  window.open = (url, ...rest) => open(url ? scopedUrl(url) : url, ...rest);

  let deadline = performance.now() + 20000;
  let timer;
  let counter = 0;
  let parentNonce = "";
  function arm() {
    clearTimeout(timer);
    timer = setTimeout(expire, Math.max(0, deadline - performance.now()));
  }
  const check = async () => {
    if (expired) return;
    const id = ++counter;
    try {
      const response = await nativeFetch("/_nexpoly/status", {
        headers: { "X-Nexpoly-Page": page }, cache: "no-store", credentials: "same-origin",
        signal: AbortSignal.timeout(3000),
      });
      if (expired || id !== counter) return;
      if ([401, 403].includes(response.status)) return expire();
      if (response.ok) {
        const grant = await response.json();
        if (expired || id !== counter) return;
        if (grant.authorized === true && Number.isFinite(grant.valid_until_ms)) {
          const remaining = grant.valid_until_ms - Date.now();
          if (remaining <= 0) return expire();
          deadline = performance.now() + Math.min(20000, remaining);
          arm();
          if (!document.hidden) {
            document.documentElement.style.visibility = "";
            if (parentNonce && parentOrigin && parent !== window) {
              parent.postMessage({ type: "nexpoly:openscience:verified", page_context: page, nonce: parentNonce }, parentOrigin);
              parentNonce = "";
            }
          }
        }
      }
    } catch { /* The last confirmed deadline remains in force. */ }
  };
  function conceal() {
    ++counter;
    document.documentElement.style.visibility = "hidden";
  }
  window.addEventListener("message", event => {
    const data = event.data;
    if (event.source !== parent || event.origin !== parentOrigin || data?.type !== "nexpoly:openscience:recheck" ||
        data.page_context !== page || typeof data.nonce !== "string") return;
    parentNonce = data.nonce;
    void check();
  });
  window.addEventListener("pagehide", conceal);
  window.addEventListener("pageshow", event => { if (event.persisted) void check(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) conceal();
    else void check();
  });
  window.addEventListener("focus", check);
  arm();
  setInterval(check, 4000);
  void check();
})();
