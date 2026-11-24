import { useEffect, useRef, useState, type Ref } from "react";
import { privateFetch } from "../auth/session";
import { withAuthMutationLock } from "../auth/authMutationLock";
import { randomId } from "../auth/randomId";
import { AUTH_BROWSER_ID_KEY } from "../auth/storage";

type Props = {
  url: string;
  iframeRef?: Ref<HTMLIFrameElement>;
  onLoad?: () => void;
  onVerified?: () => void;
  onUnavailable?: () => void;
};

export const privateWorkspaceEnabled = () => import.meta.env.VITE_OPENSCIENCE_PER_USER === "true";

function browserGroup(signal: AbortSignal) {
  return withAuthMutationLock(async () => {
    const existing = localStorage.getItem(AUTH_BROWSER_ID_KEY);
    if (existing && /^[0-9a-f-]{36}$/.test(existing)) return existing;
    const id = randomId();
    localStorage.setItem(AUTH_BROWSER_ID_KEY, id);
    return id;
  }, signal);
}

export function PrivateAgentFrame({ url, iframeRef, onLoad, onVerified, onUnavailable }: Props) {
  const frame = useRef<HTMLIFrameElement | null>(null);
  const [name] = useState(() => "nexpoly-agent-" + randomId());
  const [destination, setDestination] = useState("");
  const [error, setError] = useState("");
  const [cleanupUrl, setCleanupUrl] = useState("");
  const [attempt, setAttempt] = useState(0);
  const unavailable = useRef(onUnavailable);
  unavailable.current = onUnavailable;
  const verified = useRef(onVerified);
  verified.current = onVerified;
  const verifyLoadedFrame = useRef<() => void>(() => {});
  useEffect(() => {
    const controller = new AbortController();
    const origin = new URL(url).origin;
    let handoff = "";
    let context = "";
    let released = false;
    let visibilityNonce = "";
    let active = true;
    let cleanupOnly = false;
    const fail = (message: string) => {
      if (!active) return;
      active = false;
      controller.abort();
      clearTimeout(timeout);
      setError(message);
      setCleanupUrl("");
      unavailable.current?.();
    };
    const timedOut = () => fail("工作区启动超时，请重试。");
    let timeout = window.setTimeout(timedOut, 60_000);
    function receive(event: MessageEvent) {
      if (!active || !frame.current || event.origin !== origin || event.source !== frame.current.contentWindow) return;
      const data = event.data;
      if (!data || typeof data !== "object") return;
      if (context && data.type === "nexpoly:openscience:verified" && data.page_context === context &&
          visibilityNonce && data.nonce === visibilityNonce && !document.hidden) {
        visibilityNonce = "";
        if (!cleanupOnly) verified.current?.();
        return;
      }
      if (context && data.type === "nexpoly:openscience:expired" && data.page_context === context) {
        fail("工作区授权已失效，请重新验证。");
      }
      if (context || !handoff || handoff !== data.handoff_id) return;
      if (data.type === "nexpoly:openscience:cleanup" && typeof data.page_context === "string" && data.page_context) {
        context = data.page_context;
        cleanupOnly = true;
        clearTimeout(timeout);
        const target = new URL("/_nexpoly/cleanup", origin);
        target.searchParams.set("np_page", context);
        setCleanupUrl(target.href);
        unavailable.current?.();
        return;
      }
      if (data.type === "nexpoly:openscience:error") {
        if (data.reason === "workspace_capacity") fail("工作区容量已满，请稍后重试。");
        else if (data.reason === "workspace_timeout") timedOut();
        else if (data.reason === "workspace_unavailable" || data.reason === "workspace_transport_unavailable")
          fail("工作区暂时不可用，请稍后重试。");
        else fail("工作区授权失败，请重试。");
      }
      if (data.type === "nexpoly:openscience:ready" && typeof data.page_context === "string" && data.page_context) {
        context = data.page_context;
        const target = new URL(url);
        target.searchParams.set("np_page", context);
        clearTimeout(timeout);
        setDestination(target.href);  // This navigation originates in the trusted parent.
      }
    }
    const conceal = () => { visibilityNonce = ""; unavailable.current?.(); };
    const release = () => {
      if (!context || released) return;
      released = true;
      const endpoint = new URL("/_nexpoly/auth/leave", origin).href;
      const body = new URLSearchParams({page_context: context});
      if (navigator.sendBeacon?.(endpoint, body)) return;
      void fetch(endpoint, {method: "POST", credentials: "include", mode: "no-cors", keepalive: true, body}).catch(() => {});
    };
    const pageHide = (event: PageTransitionEvent) => { conceal(); if (!event.persisted) release(); };
    const verify = () => {
      if (!active || !context || document.hidden) return;
      visibilityNonce = randomId();
      frame.current?.contentWindow?.postMessage({
        type: "nexpoly:openscience:recheck", page_context: context, nonce: visibilityNonce,
      }, origin);
    };
    verifyLoadedFrame.current = verify;
    const visibility = () => { if (document.hidden) conceal(); else verify(); };
    const restored = (event: PageTransitionEvent) => { if (event.persisted) verify(); };
    window.addEventListener("message", receive);
    window.addEventListener("pagehide", pageHide);
    window.addEventListener("pageshow", restored);
    document.addEventListener("visibilitychange", visibility);
    void (async () => {
      const signal = controller.signal;
      const browser = await browserGroup(signal);
      const response = await privateFetch(`${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/openscience/workspace-ticket`, {
        method: "POST", body: "{}", headers: { "Content-Type": "application/json" }, signal,
      });
      if (!response.ok) {
        await response.text();
        throw new Error(response.status === 403 ? "当前账号未获准使用此工作区。" : "无法取得工作区票据，请重试。");
      }
      const data = await response.json();
      if (!active || !frame.current) return;
      const exchange = new URL("/_nexpoly/auth/exchange", origin);
      if (data.exchange_url !== exchange.href || !data.ticket || !data.handoff_id) throw new Error("工作区来源验证失败。");
      // Ticket redemption still starts immediately. Only the hidden frame's
      // wait for an authorized, ready workspace uses the supervisor's budget.
      if (Number.isInteger(data.startup_timeout_seconds) && data.startup_timeout_seconds > 0 && data.startup_timeout_seconds <= 605) {
        clearTimeout(timeout);
        timeout = window.setTimeout(timedOut, (data.startup_timeout_seconds + 10) * 1000);
      }
      handoff = data.handoff_id;
      const form = document.createElement("form");
      form.method = "POST";
      form.action = exchange.href;
      form.target = name;
      for (const [key, value] of Object.entries({ticket: String(data.ticket), handoff_id: handoff, browser_id: browser})) {
        const input = document.createElement("input");
        input.type = "hidden"; input.name = key; input.value = value;
        form.append(input);
      }
      document.body.append(form);
      try { form.submit(); } finally { form.remove(); }
    })().catch(e => fail(e instanceof Error ? e.message : "工作区启动失败，请重试。"));
    return () => {
      release();
      active = false;
      controller.abort();
      clearTimeout(timeout);
      window.removeEventListener("message", receive);
      window.removeEventListener("pagehide", pageHide);
      window.removeEventListener("pageshow", restored);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [attempt, name, url]);
  return <>
    {(error || cleanupUrl) && <div role="alert">{error || "工作区容量已满，请稍后重试。"}
      {cleanupUrl && !destination && <button onClick={() => setDestination(cleanupUrl)}>清理我的项目文件</button>}
      {" "}<button onClick={() => {
        setError(""); setDestination(""); setCleanupUrl(""); setAttempt(value => value + 1);
      }}>重试</button></div>}
    {!error && <>
      {!destination && !cleanupUrl && <div role="status">正在启动个人工作区…</div>}
      <iframe name={name} ref={element => {
        frame.current = element;
        if (typeof iframeRef === "function") iframeRef(element);
        else if (iframeRef) iframeRef.current = element;
      }} title={cleanupUrl ? "我的项目文件" : "智聚万物智能体工作台"} hidden={!destination} referrerPolicy="origin"
        src={destination || "about:blank"} onLoad={() => {
          if (destination) { verifyLoadedFrame.current(); if (!cleanupUrl) onLoad?.(); }
        }}
        className="np-member-conversation__workspace block w-full border-0 bg-white" />
    </>}
  </>;
}
