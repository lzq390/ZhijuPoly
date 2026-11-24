// Shared by the M2 login and M6 operator browser drills.
import assert from "node:assert/strict";
import { setTimeout as sleep } from "node:timers/promises";

export async function monitor(agent, c, pageContext, markers = []) {
  // This browser document stays loaded after the UI disappears. EOF therefore measures
  // server-side stream termination independently of the tab unloading its EventSource.
  const probe = await c.newPage();
  await probe.goto(agent + "/_nexpoly/expired");
  let ended;
  const end = new Promise((resolve) => {
    ended = resolve;
  });
  await probe.exposeFunction("recordStreamEnd", () => ended(performance.now()));
  await probe.evaluate(
    async ({ key, markers }) => {
      const r = await fetch("/openscience-api/global/event", {
        headers: { "X-Nexpoly-Page": key },
        cache: "no-store",
      });
      if (r.status !== 200 || !r.headers.get("Content-Type")?.includes("text/event-stream"))
        throw Error("Authorized SSE did not open");
      const reader = r.body.getReader();
      if ((await reader.read()).done) throw Error("SSE ended before revocation");
      const decoder = new TextDecoder();
      window.seenMarkers = markers.map(() => false);
      void (async () => {
        let tail = "";
        try {
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            tail += decoder.decode(value, { stream: true });
            markers.forEach((m, i) => {
              if (tail.includes(m)) window.seenMarkers[i] = true;
            });
            tail = tail.slice(-160);
          }
        } catch {
        } finally {
          await window.recordStreamEnd();
        }
      })();
    },
    { key: pageContext, markers },
  );
  const status = () =>
    probe.evaluate(
      async (key) =>
        (await fetch("/_nexpoly/status", { headers: { "X-Nexpoly-Page": key }, cache: "no-store" })).status,
      pageContext,
    );
  const reconnect = () =>
    probe.evaluate(async (key) => {
      const r = await fetch("/openscience-api/global/event", {
        headers: { "X-Nexpoly-Page": key },
        signal: AbortSignal.timeout(5000),
      });
      await r.body?.cancel();
      return r.status;
    }, pageContext);
  return { probe, end, status, reconnect };
}
export async function within(fn, description) {
  const deadline = performance.now() + 30000;
  while (performance.now() < deadline) {
    if (await fn()) return performance.now();
    await sleep(100);
  }
  throw Error(description + " exceeded 30 seconds");
}
export async function losses(start, m, old, denial = [401, 403], sidebarMarkers = ["M1 synthetic ", "same-project"]) {
  const elapsed = (t) => Math.ceil(t - start);
  const [stream, denied, tab, sidebar] = await Promise.all([
    Promise.race([
      m.end,
      sleep(30000, undefined, { ref: false }).then(() => {
        throw Error("SSE remained open");
      }),
    ]),
    within(async () => denial.includes(await m.status()), "Old context"),
    within(async () => old.isDetached() || old.url().includes("/_nexpoly/expired"), "Old tab"),
    within(async () => {
      const text = await old.page().locator(".np-sidebar-desktop").innerText();
      return sidebarMarkers.every(marker => !text.includes(marker));
    }, "Private sidebar"),
  ]);
  const result = {
    server_closed_sse_ms: elapsed(stream),
    private_page_denied_ms: elapsed(denied),
    old_document_invalid_ms: elapsed(tab),
    private_sidebar_cleared_ms: elapsed(sidebar),
  };
  assert.ok(Object.values(result).every((v) => v >= 0 && v <= 30000));
  result.stream_reconnect_status = await m.reconnect();
  assert.ok(denial.includes(result.stream_reconnect_status));
  return result;
}
export async function submitExchange(agent, page, fields, details = false) {
  // A second real form submission uses the browser's original Origin and TLS policy.
  return page.evaluate(
    ({ action, fields, details }) =>
      new Promise((resolve) => {
        const target = document.createElement("iframe");
        target.name = "m2-replay";
        target.hidden = true;
        document.body.append(target);
        const timeout = setTimeout(() => {
          window.removeEventListener("message", receive);
          target.remove();
          resolve("timed_out");
        }, 10000);
        function receive(e) {
          if (e.origin !== new URL(action).origin || e.source !== target.contentWindow ||
              e.data?.handoff_id !== new URLSearchParams(fields).get("handoff_id")) return;
          clearTimeout(timeout);
          window.removeEventListener("message", receive);
          target.remove();
          resolve(details ? {type:e.data.type, reason:e.data.reason ?? null} : e.data.type);
        }
        window.addEventListener("message", receive);
        const form = document.createElement("form");
        form.method = "POST";
        form.target = target.name;
        form.action = action;
        for (const [name, value] of new URLSearchParams(fields)) {
          const input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          input.value = value;
          form.append(input);
        }
        document.body.append(form);
        form.submit();
        form.remove();
      }),
    { action: agent + "/_nexpoly/auth/exchange", fields, details },
  );
}
export async function holdTicket(page) {
  return page.evaluate(async () => {
    const session = await (await fetch("/api/v1/auth/session")).json();
    const r = await fetch("/api/v1/openscience/workspace-ticket", {
      method: "POST",
      headers: { "X-Session-Context": session.session_id, "X-CSRF-Token": session.csrf_token },
    });
    if (!r.ok) throw Error("Expected a valid, unused workspace ticket");
    const data = await r.json();
    return {
      ttl: data.expires_in,
      fields: new URLSearchParams({
        ticket: data.ticket,
        handoff_id: data.handoff_id,
        browser_id: localStorage.getItem("nexpoly.auth.browser-id"),
      }).toString(),
    };
  });
}
