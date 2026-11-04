import { useEffect, useRef, useState } from "react";
import { confirmSimulationCheckout, readSimulationCheckout, SimulationCheckoutError, simulationTokenFromHash, type SimulationCheckout } from "../services/billingSimulation";
import "./SimulatedPaymentPage.css";

type Phase = "loading" | "pending" | "confirming" | "paid" | "invalid" | "expired" | "closed" | "unavailable";
const date = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
const titles: Record<Phase, string> = {
  loading: "正在读取模拟订单", pending: "确认模拟支付", confirming: "正在确认模拟支付", paid: "模拟支付已确认",
  invalid: "模拟链接无效", expired: "模拟链接已过期", closed: "模拟订单已关闭", unavailable: "暂时无法确认订单状态"
};

export function SimulatedPaymentPage() {
  const [token, setToken] = useState(() => simulationTokenFromHash(window.location.hash));
  useEffect(() => {
    const changed = () => setToken(simulationTokenFromHash(window.location.hash));
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  return <SimulationConfirmation key={token ?? "invalid"} token={token} />;
}

function SimulationConfirmation({ token }: { token: string | null }) {
  const [phase, setPhase] = useState<Phase>(token ? "loading" : "invalid");
  const [checkout, setCheckout] = useState<SimulationCheckout | null>(null);
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now());
  const offset = useRef(0), submitting = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { controller.abort(); clearInterval(timer); lifetime.current = null; };
  }, []);
  function accept(value: SimulationCheckout) {
    offset.current = Date.parse(value.server_time) - Date.now();
    setNow(Date.now()); setCheckout(value); setPhase(value.status);
  }
  function failed(reason: unknown) { setPhase(reason instanceof SimulationCheckoutError ? reason.kind : "unavailable"); }
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController(); setPhase("loading");
    // Opening, previewing and refreshing the link only reads the order.
    readSimulationCheckout(token, controller.signal).then(value => { if (!controller.signal.aborted) accept(value); })
      .catch(reason => { if (!controller.signal.aborted) failed(reason); });
    return () => controller.abort();
  }, [token, revision]);
  const expired = checkout?.status === "pending" && Date.parse(checkout.expires_at) <= now + offset.current;
  const displayedPhase = expired && phase === "pending" ? "expired" : phase;
  async function confirm() {
    if (!token || phase !== "pending" || expired || submitting.current) return;
    submitting.current = true; setPhase("confirming");
    const signal = lifetime.current?.signal;
    try { const value = await confirmSimulationCheckout(token, signal); if (!signal?.aborted) accept(value); }
    catch (reason) { if (!signal?.aborted) failed(reason); }
    finally { submitting.current = false; }
  }
  return <main className="np-simulated-payment" aria-label="开发模拟付款">
    <div className="np-simulation-brand">NEXPOLY <span>开发验收</span></div>
    <aside className="np-simulation-warning" role="note"><strong>开发模拟 · 不会扣款</strong><p>这是 Nexpoly 的模拟确认页，不是微信或支付宝官方收银台。无需登录、无需输入支付密码。</p></aside>
    <section className="np-simulation-sheet" aria-live="polite">
      <h1>{titles[displayedPhase]}</h1>
      {checkout && <>
        <p className="np-simulation-amount">¥{(checkout.amount_fen / 100).toFixed(2)}</p><p className="np-simulation-muted">仅为验收展示金额，不会收取费用</p>
        <dl><div><dt>模拟渠道</dt><dd>{checkout.provider === "wechat" ? "微信支付" : "支付宝"}（模拟）</dd></div><div><dt>会员套餐</dt><dd>月费会员 · 1 个月</dd></div><div><dt>订单尾号</dt><dd>{checkout.order_suffix}</dd></div><div><dt>确认截止</dt><dd>{date(checkout.expires_at)}<small>本地时间</small></dd></div></dl>
      </>}
      {displayedPhase === "loading" && <p role="status">正在读取订单；打开或刷新此页面不会支付。</p>}
      {(displayedPhase === "pending" || displayedPhase === "confirming") && <><p>点击确认后，将为电脑端下单账号开通或续费一个月会员。电脑页面会自动更新。</p><button type="button" disabled={displayedPhase === "confirming"} onClick={() => void confirm()}>{displayedPhase === "confirming" ? "正在确认…" : "确认模拟支付（不会扣款）"}</button></>}
      {displayedPhase === "paid" && <><p role="status">已完成模拟支付，没有发生真实扣款。请返回电脑，会员状态会自动更新。</p>{checkout?.access_ends_at && <p>会员有效期至 {date(checkout.access_ends_at)}（本地时间）。</p>}</>}
      {displayedPhase === "invalid" && <p>请从电脑端重新扫描当前模拟订单的二维码。</p>}
      {displayedPhase === "expired" && <p>链接已超过确认期限，无法继续支付。请返回电脑读取订单状态。</p>}
      {displayedPhase === "closed" && <p>此订单不能继续确认，请返回电脑查看订单状态。</p>}
      {displayedPhase === "unavailable" && <><p role="alert">结果尚未确认，请重新读取原订单；读取操作不会发起支付。</p><button type="button" onClick={() => setRevision(value => value + 1)}>重新读取</button></>}
    </section>
    <footer>此页只用于开发环境的订阅流程验收。</footer>
  </main>;
}
