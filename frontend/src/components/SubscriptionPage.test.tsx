// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BillingOrder, Subscription } from "../services/billing";
const mocks = vi.hoisted(() => ({ auth: vi.fn(), plans: vi.fn(), subscription: vi.fn(), orders: vi.fn(), create: vi.fn(), order: vi.fn(), refresh: vi.fn(), qr: vi.fn(), simulate: vi.fn() }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: mocks.auth }));
vi.mock("qrcode", () => ({ default: { toDataURL: mocks.qr } }));
vi.mock("../services/billing", () => ({ fetchBillingPlans: mocks.plans, fetchSubscription: mocks.subscription, fetchBillingOrders: mocks.orders, createBillingOrder: mocks.create, fetchBillingOrder: mocks.order, simulateBillingPayment: mocks.simulate }));
import { SubscriptionPage } from "./SubscriptionPage";
const subscription: Subscription = { account_type: "free", source: "none", expires_at: null, server_time: new Date().toISOString() };
const pending: BillingOrder = { id: "order-a", provider: "wechat", amount_fen: 9900, currency: "CNY", status: "pending", qr_code: "weixin://test", created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 3600000).toISOString(), paid_at: null, access_ends_at: null };
beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear();
  mocks.auth.mockReturnValue({ status: "authenticated", session: { user: { id: "user-a" }, account_type: "free" }, refreshSession: mocks.refresh, login: vi.fn(), register: vi.fn() });
  mocks.plans.mockResolvedValue({ enabled: true, simulation: false, monthly_price_fen: 9900, currency: "CNY", period: "month", providers: ["wechat", "alipay"], registration_enabled: true });
  mocks.subscription.mockResolvedValue(subscription); mocks.orders.mockResolvedValue([]); mocks.create.mockResolvedValue(pending); mocks.order.mockResolvedValue(pending);
  mocks.refresh.mockResolvedValue(undefined); mocks.qr.mockResolvedValue("data:image/png;base64,test");
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("does not read private orders for a guest and offers enabled registration", async () => {
  const login = vi.fn(), register = vi.fn(); mocks.auth.mockReturnValue({ status: "guest", session: {}, login, register });
  render(<SubscriptionPage />);
  fireEvent.click(await screen.findByRole("button", { name: "登录后开通" }));
  fireEvent.click(screen.getByRole("button", { name: "注册账号" }));
  expect(login).toHaveBeenCalledOnce(); expect(register).toHaveBeenCalledOnce(); expect(mocks.subscription).not.toHaveBeenCalled(); expect(mocks.orders).not.toHaveBeenCalled();
});
it("preserves legacy membership and does not offer another purchase", async () => {
  mocks.subscription.mockResolvedValue({ ...subscription, account_type: "member", source: "legacy" });
  render(<SubscriptionPage />);
  expect(await screen.findByText(/长期会员权限/)).toBeTruthy(); expect(screen.queryByRole("button", { name: "开通一个月" })).toBeNull(); expect(screen.queryByLabelText("月费会员套餐")).toBeNull();
});
it("does not sell to an administrator overridden account", async () => {
  mocks.subscription.mockResolvedValue({ ...subscription, source: "admin", purchase_blocked: true });
  render(<SubscriptionPage />);
  expect(await screen.findByText(/权限由管理员设置/)).toBeTruthy(); expect(screen.queryByLabelText("月费会员套餐")).toBeNull();
});
it("shows unavailable checkout honestly when configuration is disabled", async () => {
  mocks.plans.mockResolvedValue({ enabled: false, monthly_price_fen: null, providers: [], registration_enabled: true });
  render(<SubscriptionPage />);
  expect(await screen.findByText(/在线购买暂未开放/)).toBeTruthy(); expect(screen.queryByRole("button", { name: "开通一个月" })).toBeNull();
});
it("preserves the disabled plan when an older database cannot serve membership tables", async () => {
  mocks.plans.mockResolvedValue({ enabled: false, monthly_price_fen: null, providers: [], registration_enabled: false });
  mocks.subscription.mockRejectedValue(new Error("会员数据尚未准备好。"));
  mocks.orders.mockRejectedValue(new Error("会员数据尚未准备好。"));
  render(<SubscriptionPage />);
  expect(await screen.findByText(/在线购买暂未开放/)).toBeTruthy();
  expect(screen.getByText("订单记录暂时无法读取，请稍后重试。")).toBeTruthy();
  expect(screen.queryByText("暂无订单。")).toBeNull();
  expect(screen.queryByRole("button", { name: "开通一个月" })).toBeNull();
});
it("does not offer a purchase while existing orders cannot be confirmed", async () => {
  mocks.orders.mockRejectedValue(new Error("读取订单失败。"));
  render(<SubscriptionPage />);
  expect(await screen.findByText(/会员状态或订单记录暂时无法确认/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "开通一个月" })).toBeNull();
  expect(screen.queryByText("暂无订单。")).toBeNull();
});
it("generates QR locally and reads the original order before activating permissions", async () => {
  render(<SubscriptionPage />);
  fireEvent.click(await screen.findByRole("button", { name: "开通一个月" }));
  expect(await screen.findByAltText("微信支付二维码")).toBeTruthy();
  expect(mocks.qr).toHaveBeenCalledWith("weixin://test", expect.objectContaining({ width: 256 }));
  expect(mocks.refresh).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "模拟支付成功（不会扣款）" })).toBeNull();
  const paid = { ...pending, status: "paid", access_ends_at: "2027-01-01T00:00:00Z" };
  mocks.order.mockResolvedValue(paid); mocks.subscription.mockResolvedValue({ ...subscription, account_type: "member", source: "subscription", expires_at: paid.access_ends_at });
  fireEvent.click(screen.getByRole("button", { name: "我已支付，读取结果" }));
  await screen.findByText(/会员已生效/); expect(mocks.refresh).toHaveBeenCalledOnce(); expect(mocks.create).toHaveBeenCalledOnce();
});
it("reuses its idempotency key after an uncertain create and prevents another pending order", async () => {
  mocks.create.mockRejectedValueOnce(new Error("网络中断")).mockResolvedValueOnce({ ...pending, qr_code: null });
  render(<SubscriptionPage />);
  fireEvent.click(await screen.findByRole("button", { name: "开通一个月" }));
  await screen.findByText("网络中断");
  expect((screen.getByRole("radio", { name: "支付宝" }).closest("fieldset") as HTMLFieldSetElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "确认原订单" }));
  await screen.findByText("支付渠道确认中，请读取当前订单。");
  expect(mocks.create.mock.calls[0][1]).toBe(mocks.create.mock.calls[1][1]);
  expect((screen.getByRole("button", { name: "开通一个月" }) as HTMLButtonElement).disabled).toBe(true);
  expect(JSON.parse(sessionStorage.getItem("nexpoly.billing.purchase.user-a.wechat")!).orderId).toBe(pending.id);
});
it("polls a pending payment without automatically creating a replacement", async () => {
  mocks.orders.mockResolvedValue([pending]);
  render(<SubscriptionPage />); await screen.findByRole("heading", { name: "等待支付" });
  await waitFor(() => expect(mocks.order).toHaveBeenCalledWith(pending.id, expect.any(AbortSignal)), { timeout: 3000 });
  expect(mocks.create).not.toHaveBeenCalled();
});
it("does not refresh another account from an order read after unmount", async () => {
  mocks.orders.mockResolvedValue([pending]); let finish!: (value: BillingOrder) => void;
  mocks.order.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<SubscriptionPage />); fireEvent.click(await screen.findByRole("button", { name: "我已支付，读取结果" }));
  view.unmount(); await act(async () => finish({ ...pending, status: "paid" }));
  expect(mocks.refresh).not.toHaveBeenCalled();
});
const simulationToken = `${"a".repeat(32)}.${"b".repeat(43)}`;
it.each(["wechat", "alipay"] as const)("shows a current-origin %s simulation QR and lets desktop polling refresh membership", async provider => {
  mocks.plans.mockResolvedValue({ enabled: true, simulation: true, monthly_price_fen: 9900, providers: ["wechat", "alipay"], registration_enabled: true });
  mocks.orders.mockResolvedValue([{ ...pending, provider, simulation_checkout_token: simulationToken }]);
  render(<SubscriptionPage />);
  expect(await screen.findByAltText(`${provider === "wechat" ? "微信" : "支付宝"}模拟扫码二维码`)).toBeTruthy();
  const url = `${window.location.origin}/billing/simulate-pay#token=${simulationToken}`;
  expect(mocks.qr).toHaveBeenCalledWith(url, expect.objectContaining({ width: 256 }));
  const link = screen.getByRole("link", { name: "在新窗口打开模拟确认页" });
  expect(link.getAttribute("href")).toBe(url); expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  expect(screen.queryByRole("button", { name: "模拟支付成功（不会扣款）" })).toBeNull();
  expect(mocks.simulate).not.toHaveBeenCalled(); expect(mocks.refresh).not.toHaveBeenCalled();
  expect(screen.getByText(/当前为本机地址，手机无法访问/)).toBeTruthy();
  mocks.subscription.mockResolvedValue({ ...subscription, account_type: "member", source: "subscription", expires_at: "2027-01-01T00:00:00Z" });
  mocks.order.mockResolvedValue({ ...pending, provider, status: "paid", access_ends_at: "2027-01-01T00:00:00Z" });
  await screen.findByText(/会员已生效/, {}, { timeout: 3000 });
  expect(mocks.refresh).toHaveBeenCalledOnce(); expect(mocks.create).not.toHaveBeenCalled(); expect(mocks.simulate).not.toHaveBeenCalled();
});
it("reads an existing pending simulation order to recover its token without creating a replacement", async () => {
  mocks.plans.mockResolvedValue({ enabled: true, simulation: true, monthly_price_fen: 9900, providers: ["wechat"], registration_enabled: true });
  mocks.orders.mockResolvedValue([{ ...pending, qr_code: null }]);
  mocks.order.mockResolvedValue({ ...pending, simulation_checkout_token: simulationToken });
  render(<SubscriptionPage />);
  await screen.findByText("正在准备模拟扫码链接，请读取当前订单。"); expect(mocks.qr).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "读取模拟结果" }));
  await screen.findByAltText("微信模拟扫码二维码");
  expect(mocks.order).toHaveBeenCalledWith(pending.id, expect.any(AbortSignal)); expect(mocks.create).not.toHaveBeenCalled();
});
it("does not expose the simulation QR or confirmation link after expiry", async () => {
  mocks.plans.mockResolvedValue({ enabled: true, simulation: true, monthly_price_fen: 9900, providers: ["wechat"], registration_enabled: true });
  mocks.orders.mockResolvedValue([{ ...pending, simulation_checkout_token: simulationToken, expires_at: new Date(Date.now() - 60000).toISOString() }]);
  render(<SubscriptionPage />);
  await screen.findByText("模拟订单已过期，请读取订单确认状态。");
  expect(screen.queryByRole("img")).toBeNull(); expect(screen.queryByRole("link")).toBeNull(); expect(mocks.simulate).not.toHaveBeenCalled();
});

it.each([
  ["wechat", "paid"], ["alipay", "paid"], ["wechat", "closed"], ["alipay", "closed"]
] as const)("retires a completed %s purchase key restored as %s before starting a new purchase", async (provider, status) => {
  const storageKey = `nexpoly.billing.purchase.user-a.${provider}`;
  const otherProvider = provider === "wechat" ? "alipay" : "wechat";
  const otherKey = `nexpoly.billing.purchase.user-a.${otherProvider}`;
  sessionStorage.setItem(storageKey, JSON.stringify({ key: "previous-request", orderId: pending.id }));
  sessionStorage.setItem(otherKey, JSON.stringify({ key: "another-request", orderId: "unconfirmed-order" }));
  mocks.plans.mockResolvedValue({ enabled: true, simulation: false, monthly_price_fen: 9900, providers: [provider], registration_enabled: true });
  mocks.orders.mockResolvedValue([{ ...pending, provider, status }]);
  mocks.create.mockResolvedValue({ ...pending, id: "new-order", provider });
  if (status === "paid") mocks.subscription.mockResolvedValue({ ...subscription, account_type: "member", source: "subscription", expires_at: "2027-01-01T00:00:00Z" });
  render(<SubscriptionPage />);
  fireEvent.click(await screen.findByRole("button", { name: status === "paid" ? "续费一个月" : "开通一个月" }));
  await screen.findByRole("heading", { name: "等待支付" });
  expect(mocks.create).toHaveBeenCalledOnce();
  expect(mocks.create.mock.calls[0][1]).not.toBe("previous-request");
  expect(JSON.parse(sessionStorage.getItem(storageKey)!).orderId).toBe("new-order");
  expect(JSON.parse(sessionStorage.getItem(otherKey)!)).toEqual({ key: "another-request", orderId: "unconfirmed-order" });
});

it("retires only terminal keys while preserving pending and uncertain purchases on restore", async () => {
  sessionStorage.setItem("nexpoly.billing.purchase.user-a.wechat", JSON.stringify({ key: "pending-key", orderId: pending.id }));
  sessionStorage.setItem("nexpoly.billing.purchase.user-a.alipay", JSON.stringify({ key: "uncertain-key" }));
  mocks.orders.mockResolvedValue([pending, { ...pending, id: "other-completed", provider: "alipay", status: "paid" }]);
  render(<SubscriptionPage />);
  await screen.findByRole("heading", { name: "等待支付" });
  expect(JSON.parse(sessionStorage.getItem("nexpoly.billing.purchase.user-a.wechat")!)).toEqual({ key: "pending-key", orderId: pending.id });
  expect(JSON.parse(sessionStorage.getItem("nexpoly.billing.purchase.user-a.alipay")!)).toEqual({ key: "uncertain-key" });
  expect(mocks.create).not.toHaveBeenCalled();
});

it("recovers each channel independently when another channel has an uncertain request", async () => {
  sessionStorage.setItem("nexpoly.billing.purchase.user-a.wechat", JSON.stringify({ key: "uncertain-wechat" }));
  sessionStorage.setItem("nexpoly.billing.purchase.user-a.alipay", JSON.stringify({ key: "completed-alipay", orderId: "paid-alipay" }));
  mocks.orders.mockResolvedValue([{ ...pending, id: "paid-alipay", provider: "alipay", status: "paid" }]);
  render(<SubscriptionPage />);
  await screen.findByRole("button", { name: "确认原订单" });
  expect(JSON.parse(sessionStorage.getItem("nexpoly.billing.purchase.user-a.wechat")!)).toEqual({ key: "uncertain-wechat" });
  expect(sessionStorage.getItem("nexpoly.billing.purchase.user-a.alipay")).toBeNull();
  expect((screen.getByRole("radio", { name: "微信支付" }) as HTMLInputElement).checked).toBe(true);
  expect(mocks.create).not.toHaveBeenCalled();
});
