import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { Crown, RefreshCw, ShieldCheck } from "lucide-react";
import { useAuth } from "../auth/AuthProvider";
import { randomId } from "../auth/randomId";
import { createBillingOrder, fetchBillingOrder, fetchBillingOrders, fetchBillingPlans, fetchSubscription, type BillingOrder, type BillingPlans, type PaymentProvider, type Subscription } from "../services/billing";
import { simulationCheckoutUrl } from "../services/billingSimulation";
import { ModulePageHeader } from "./ModulePageHeader";
import "./SubscriptionPage.css";

const providers: Record<PaymentProvider, string> = { wechat: "微信支付", alipay: "支付宝" };
const statuses: Record<BillingOrder["status"], string> = { pending: "等待支付", paid: "支付成功", closed: "订单已关闭", failed: "支付失败" };
const money = (fen: number) => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(fen / 100);
const date = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const message = (error: unknown) => error instanceof Error ? error.message : "操作失败，请重试。";

export function SubscriptionPage({ onReturn }: { onReturn?: () => void } = {}) {
  const auth = useAuth();
  return <section className="np-module-page np-subscription-page">
    <ModulePageHeader>会员订阅</ModulePageHeader>
    <SubscriptionContent key={auth?.session.user?.id ?? "guest"} onReturn={onReturn} />
  </section>;
}

function SubscriptionContent({ onReturn }: { onReturn?: () => void }) {
  const auth = useAuth();
  const signedIn = auth?.status === "authenticated";
  const [plans, setPlans] = useState<BillingPlans | null>(null);
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [orders, setOrders] = useState<BillingOrder[]>([]);
  const [ordersReady, setOrdersReady] = useState(false);
  const [order, setOrder] = useState<BillingOrder | null>(null);
  const [provider, setProvider] = useState<PaymentProvider>("wechat");
  const [uncertainProvider, setUncertainProvider] = useState<PaymentProvider | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [qr, setQr] = useState("");
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now());
  const clockOffset = useRef(0);
  const lifetime = useRef<AbortController | null>(null);
  const refreshSession = useRef(auth?.refreshSession);
  refreshSession.current = auth?.refreshSession;
  const confirmed = useRef(new Set<string>());
  const syncing = useRef(new Set<string>());
  const submitting = useRef(false);
  useEffect(() => { const controller = new AbortController(); lifetime.current = controller; return () => { controller.abort(); lifetime.current = null; }; }, []);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(""); setOrdersReady(false);
    Promise.allSettled([fetchBillingPlans(controller.signal), signedIn ? fetchSubscription(controller.signal) : null, signedIn ? fetchBillingOrders(controller.signal) : []])
      .then(([planResult, subscriptionResult, ordersResult]) => {
        if (controller.signal.aborted) return;
        const nextPlans = planResult.status === "fulfilled" ? planResult.value : null;
        const nextSubscription = subscriptionResult.status === "fulfilled" ? subscriptionResult.value : null;
        const nextOrders = ordersResult.status === "fulfilled" ? ordersResult.value : null;
        setPlans(nextPlans); setSubscription(nextSubscription); setOrdersReady(nextOrders !== null);
        const failures = [planResult, subscriptionResult, ordersResult].flatMap(result => result.status === "rejected" ? [message(result.reason)] : []);
        setError([...new Set(failures)].join(" "));
        if (nextSubscription) clockOffset.current = new Date(nextSubscription.server_time).getTime() - Date.now();
        if (nextPlans) setProvider(current => nextPlans.providers.includes(current) ? current : nextPlans.providers[0] ?? "wechat");
        if (signedIn) {
          for (const value of ["wechat", "alipay"] as const) {
            try {
              const saved = JSON.parse(sessionStorage.getItem(`nexpoly.billing.purchase.${auth!.session.user!.id}.${value}`) ?? "null");
              if (saved?.key && !saved.orderId) { setUncertainProvider(value); setProvider(value); break; }
            } catch { /* New purchases fail before dispatch if safe persistence is unavailable. */ }
          }
        }
        if (nextOrders) {
          setOrders(nextOrders);
          setOrder(current => current ? nextOrders.find(item => item.id === current.id) ?? current : nextOrders.find(item => item.status === "pending") ?? null);
        }
      }).catch(reason => { if (!controller.signal.aborted) setError(message(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [signedIn, revision]);

  async function applyOrder(next: BillingOrder, signal?: AbortSignal) {
    if (signal?.aborted) return;
    if (next.status !== "pending" && auth?.session.user?.id) {
      const key = `nexpoly.billing.purchase.${auth.session.user.id}.${next.provider}`;
      try { if (JSON.parse(sessionStorage.getItem(key) ?? "null")?.orderId === next.id) sessionStorage.removeItem(key); } catch { /* A retained key can only return its original order. */ }
    }
    setOrder(next); setOrders(current => [next, ...current.filter(item => item.id !== next.id)]); setPollError("");
    if (next.status !== "paid" || confirmed.current.has(next.id) || syncing.current.has(next.id)) return;
    syncing.current.add(next.id);
    try {
      const syncSignal = lifetime.current?.signal;
      await refreshSession.current?.();
      if (syncSignal?.aborted) return;
      const value = await fetchSubscription(syncSignal);
      if (!syncSignal?.aborted) { setSubscription(value); confirmed.current.add(next.id); }
    } catch (reason) { if (lifetime.current && !lifetime.current.signal.aborted) setPollError(`支付已确认，会员状态同步未完成：${message(reason)}`); }
    finally { syncing.current.delete(next.id); }
  }
  // Continue reading the same order after a timeout; never create another order automatically.
  useEffect(() => {
    if (!order || order.status !== "pending") return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (document.visibilityState !== "hidden") {
        try { await applyOrder(await fetchBillingOrder(order.id, controller.signal), controller.signal); }
        catch (reason) { if (!controller.signal.aborted) setPollError(`暂未确认支付状态：${message(reason)}`); }
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    };
    timer = setTimeout(poll, 1500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [order?.id, order?.status]);
  const simulationUrl = plans?.simulation && order?.simulation_checkout_token ? simulationCheckoutUrl(order.simulation_checkout_token, window.location.origin) : null;
  const qrPayload = plans?.simulation ? simulationUrl : order?.qr_code;
  useEffect(() => {
    setQr(""); let active = true;
    if (order?.status === "pending" && qrPayload) QRCode.toDataURL(qrPayload, { width: 256, margin: 2, errorCorrectionLevel: "M" })
      .then(value => { if (active) setQr(value); }).catch(() => { if (active) setPollError("无法显示支付二维码，请重新读取订单。"); });
    return () => { active = false; };
  }, [order?.id, order?.status, qrPayload]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);

  async function buy() {
    if (!signedIn || submitting.current || !plans?.enabled || !subscription || !ordersReady || subscription.source === "legacy" || subscription.purchase_blocked) return;
    submitting.current = true; setBusy(true); setError("");
    const signal = lifetime.current?.signal;
    try {
      const storageKey = `nexpoly.billing.purchase.${auth!.session.user!.id}.${provider}`;
      const pending = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
      const key = typeof pending?.key === "string" ? pending.key : `billing-${randomId()}`;
      sessionStorage.setItem(storageKey, JSON.stringify({ key, orderId: pending?.orderId }));
      setUncertainProvider(provider);
      const next = await createBillingOrder(provider, key, signal);
      sessionStorage.setItem(storageKey, JSON.stringify({ key, orderId: next.id }));
      setUncertainProvider(null);
      await applyOrder(next, signal);
    } catch (reason) { if (!signal?.aborted) setError(message(reason)); }
    finally { submitting.current = false; if (!signal?.aborted) setBusy(false); }
  }
  async function checkOrder(id: string) {
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setPollError("");
    const signal = lifetime.current?.signal;
    try { confirmed.current.delete(id); await applyOrder(await fetchBillingOrder(id, signal), signal); }
    catch (reason) { if (!signal?.aborted) setPollError(message(reason)); }
    finally { submitting.current = false; if (!signal?.aborted) setBusy(false); }
  }
  const legacy = subscription?.source === "legacy" && subscription.account_type === "member";
  const blocked = subscription?.purchase_blocked === true;
  const expiredOrder = order ? new Date(order.expires_at).getTime() <= now + clockOffset.current : false;
  const activePending = orders.some(item => item.status === "pending");
  const available = plans?.enabled && typeof plans.monthly_price_fen === "number" && plans.monthly_price_fen > 0 && plans.providers.length > 0;
  return <div className="np-subscription-body">
    <header className="np-subscription-intro"><Crown aria-hidden="true" /><div><h2>为材料研究开通会员</h2><p>按月购买，手动续费，不会自动扣款。</p></div></header>
    {loading && <p role="status">正在读取会员信息…</p>}
    {error && <div className="np-subscription-error" role="alert"><p>{error}</p><button type="button" disabled={busy} onClick={() => setRevision(value => value + 1)}>重新读取</button></div>}
    {!loading && plans && <>
      {plans.simulation && <aside className="np-subscription-simulation" role="note"><strong>开发模拟支付 · 不会扣款</strong><p>当前金额仅供流程验收；扫码会打开 Nexpoly 模拟确认页，微信和支付宝均不会真实扣款。</p></aside>}
      {signedIn && subscription && <section className="np-subscription-card" aria-label="当前会员状态"><h3>当前账号</h3><strong>{subscription.account_type === "member" ? "会员账号" : "普通账号"}</strong>
        <p>{legacy ? "当前账号已拥有长期会员权限，无需重复购买。" : subscription.expires_at ? `会员有效期至 ${date(subscription.expires_at)}（本地时间）` : "可使用本地画板和公开示例，开通会员后使用科研服务。"}</p>
        {blocked && <p>当前权限由管理员设置，暂不提供在线购买。</p>}
      </section>}
      {!legacy && !blocked && <section className="np-subscription-card np-subscription-plan" aria-label="月费会员套餐"><div><span className="np-subscription-eyebrow">NEXPOLY MEMBER</span><h3>月费会员</h3><p className="np-subscription-price">{plans.monthly_price_fen && plans.monthly_price_fen > 0 ? money(plans.monthly_price_fen) : "价格待开放"}<small> / 月{plans.simulation ? "（模拟展示价）" : ""}</small></p></div>
        <ul><li><ShieldCheck aria-hidden="true" />科研检索、预测与计算服务</li><li><ShieldCheck aria-hidden="true" />使用科研工作空间与任务管理</li><li><ShieldCheck aria-hidden="true" />续费时延长现有有效期</li></ul>
        {!available ? <p role="status">在线购买暂未开放。价格及支付渠道准备完成后，可在此开通会员。</p> : !signedIn ? <div className="np-subscription-actions"><button type="button" className="is-primary" onClick={() => auth?.login()}>登录后开通</button>{plans.registration_enabled && <button type="button" onClick={() => auth?.register()}>注册普通账号</button>}</div> : !subscription || !ordersReady ? <p role="status">会员状态或订单记录暂时无法确认，请重新读取后再开通。</p> : <>
          <fieldset disabled={busy || activePending || Boolean(uncertainProvider)}><legend>支付方式</legend>{plans.providers.map(value => <label key={value}><input type="radio" name="payment-provider" value={value} checked={provider === value} onChange={() => setProvider(value)} />{providers[value]}{plans.simulation ? "（开发模拟）" : ""}</label>)}</fieldset>
          <button type="button" className="is-primary" disabled={busy || !subscription || activePending} onClick={() => void buy()}>{busy ? "正在处理…" : uncertainProvider ? "确认原订单" : subscription?.account_type === "member" ? "续费一个月" : "开通一个月"}</button>
          {uncertainProvider && <p role="status">上次订单提交结果尚未确认，请使用同一支付方式确认原订单。</p>}
          {activePending && <p role="status">已有待确认订单，请先读取该订单的支付结果。</p>}
          <p className="np-subscription-note">{plans.simulation ? "模拟支付确认后生效" : "支付后生效"}；会员到期后恢复普通账号权限，账户及历史数据保留。</p>
        </>}
      </section>}
      {order && <section className="np-subscription-card np-subscription-payment" aria-label="支付订单"><h3>{plans.simulation ? "模拟订单 · " : ""}{statuses[order.status]}</h3><p>{providers[order.provider]}{plans.simulation ? "（开发模拟）" : ""} · {money(order.amount_fen)}</p>
        {order.status === "pending" && !expiredOrder && <>
          {qr ? <img src={qr} width={256} height={256} alt={plans.simulation ? `${order.provider === "wechat" ? "微信" : "支付宝"}模拟扫码二维码` : `${providers[order.provider]}二维码`} /> : <p role="status">{qrPayload ? "正在生成支付二维码…" : plans.simulation ? "正在准备模拟扫码链接，请读取当前订单。" : "支付渠道确认中，请读取当前订单。"}</p>}
          {plans.simulation && simulationUrl && <div className="np-subscription-scan"><p>请使用手机{providers[order.provider]}扫一扫，在打开的 Nexpoly 页面确认模拟支付。无需登录，确认后电脑会自动更新会员状态。</p><a href={simulationUrl} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">在新窗口打开模拟确认页</a><p className="np-subscription-note">仅供开发验收；新窗口也可测试确认流程。</p>{/^(localhost|127\.[\d.]+|\[::1\]|.*\.localhost)$/.test(window.location.hostname) && <p role="note">当前为本机地址，手机无法访问。请用手机可访问的站点域名或局域网地址重新打开订阅页，再扫码。</p>}</div>}
        </>}
        {order.status === "pending" && <p>{plans.simulation ? expiredOrder ? "模拟订单已过期，请读取订单确认状态。" : `此订单仅用于开发验收，不会扣款；有效至 ${date(order.expires_at)}。` : expiredOrder ? "二维码已过期。如已支付，请读取订单确认结果。" : `请使用${providers[order.provider]}扫码，二维码有效至 ${date(order.expires_at)}。`}</p>}
        {order.status === "paid" && <p role="status">{subscription?.account_type === "member" ? "会员已生效，可返回原功能继续使用。" : "支付已成功，正在确认会员状态。"}{order.access_ends_at && ` 本次开通后有效期至 ${date(order.access_ends_at)}。`}</p>}
        <p className="np-subscription-note">订单号：{order.id}</p>{order.status === "paid" && subscription?.account_type === "member" && onReturn && <button type="button" className="is-primary" onClick={onReturn}>返回原功能</button>}{pollError && <p role="alert">{pollError}</p>}
        <button type="button" disabled={busy} onClick={() => void checkOrder(order.id)}><RefreshCw aria-hidden="true" />{order.status === "paid" ? "刷新会员状态" : plans.simulation ? "读取模拟结果" : "我已支付，读取结果"}</button>
      </section>}
      {signedIn && <section className="np-subscription-card" aria-label="订单记录"><h3>订单记录{plans.simulation ? "（开发模拟）" : ""}</h3>{!ordersReady ? <p>订单记录暂时无法读取，请稍后重试。</p> : orders.length ? <div className="np-subscription-orders"><table><thead><tr><th>创建时间</th><th>金额</th><th>方式</th><th>状态</th><th>操作</th></tr></thead><tbody>{orders.map(item => <tr key={item.id}><td>{date(item.created_at)}</td><td>{money(item.amount_fen)}</td><td>{providers[item.provider]}{plans.simulation ? "（模拟）" : ""}</td><td>{statuses[item.status]}</td><td><button type="button" disabled={busy} onClick={() => void checkOrder(item.id)}>查看订单</button></td></tr>)}</tbody></table></div> : <p>暂无订单。</p>}</section>}
    </>}
  </div>;
}
