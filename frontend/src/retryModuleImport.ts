type FailedStyle = { url: URL; link?: HTMLLinkElement };
type StyleRetry = { link: HTMLLinkElement; promise: Promise<void> };
const styleRetries = new WeakMap<Document, Map<string, StyleRetry>>();

function retryStyle({ url, link: failedLink }: FailedStyle): Promise<void> {
  const href = url.href;
  const retries = styleRetries.get(document) ?? new Map<string, StyleRetry>();
  styleRetries.set(document, retries);
  const shared = retries.get(href);
  if (shared?.link.isConnected && shared.link.href === href) return shared.promise;
  const previous = [...document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')]
    .find(link => link.href === href);
  // Chromium may retain an empty sheet on the very link that emitted `error`.
  // Only a different link can represent another request or HMR recovering it.
  if (failedLink && previous !== failedLink && previous?.sheet) return Promise.resolve();
  const link = ((previous ?? failedLink)?.cloneNode() as HTMLLinkElement | undefined) ?? document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  const promise = new Promise<void>((resolve, reject) => {
    link.onload = () => { link.onload = link.onerror = null; resolve(); };
    link.onerror = () => {
      link.onload = link.onerror = null;
      link.remove();
      reject(new Error(`Unable to preload CSS for ${href}`));
    };
    if (previous) previous.replaceWith(link);
    else document.head.appendChild(link);
  });
  retries.set(href, { link, promise });
  void promise.catch(() => { if (retries.get(href)?.promise === promise) retries.delete(href); });
  return promise;
}

/** Native ESM remembers a failed import for the lifetime of the document.
 * Retry the requested module with a fresh URL, without reloading the App or its
 * shared structure document. Successful imports keep their original identity.
 */
export function retryModuleImport<T>(load: () => Promise<T>): () => Promise<T> {
  let failedModule: URL | undefined;
  const failedStyles = new Map<string, FailedStyle>();
  let attempt = 0;
  let pending: Promise<T> | undefined;
  function resource(message: string, prefix: string): URL | undefined {
    if (!message.startsWith(prefix)) return;
    try {
      const url = new URL(message.slice(prefix.length).trim(), window.location.href);
      if (url.origin === window.location.origin) return url;
    } catch { /* An evaluation error is handled by the page boundary. */ }
  }
  const request = async () => {
    // Vite waits for all preload links but only throws the first CSS failure.
    // Record actual resource errors while this request is in flight; otherwise
    // its global `seen` cache silently skips the remaining failed links on retry.
    const observedStyles = new Map<string, FailedStyle>();
    const observeStyleError = (event: Event) => {
      const link = event.target as HTMLLinkElement | null;
      if (link?.tagName !== "LINK" || link.rel !== "stylesheet") return;
      const url = resource(link.href, "");
      if (url) observedStyles.set(url.href, { url, link });
    };
    const hostDocument = typeof document === "undefined" ? undefined : document;
    hostDocument?.addEventListener("error", observeStyleError, true);
    try {
      if (failedStyles.size) {
        await Promise.all([...failedStyles].map(async ([href, style]) => {
          await retryStyle(style);
          if (failedStyles.get(href) === style) failedStyles.delete(href);
        }));
      }
      if (failedModule) {
        const url = new URL(failedModule);
        url.searchParams.set("page-retry", `${Date.now()}-${++attempt}`);
        return await import(/* @vite-ignore */ url.href) as T;
      }
      return await load();
    } catch (error) {
      const message = error instanceof Error ? error.message : "";
      failedModule = resource(message, "Failed to fetch dynamically imported module:")
        ?? resource(message, "error loading dynamically imported module:") ?? failedModule;
      const failedStyle = resource(message, "Unable to preload CSS for ");
      if (failedStyle) {
        observedStyles.forEach((style, href) => failedStyles.set(href, style));
        if (!failedStyles.has(failedStyle.href)) failedStyles.set(failedStyle.href, { url: failedStyle });
      }
      throw error;
    } finally {
      hostDocument?.removeEventListener("error", observeStyleError, true);
    }
  };
  return () => pending ??= request().catch(error => {
    pending = undefined;
    throw error;
  });
}
