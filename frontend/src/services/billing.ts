import { privateFetch } from "../auth/session";

export type PaymentProvider = "wechat" | "alipay";
export type BillingPlans = { enabled: boolean; simulation: boolean; currency: "CNY"; monthly_price_fen: number | null; period: "month"; providers: PaymentProvider[]; registration_enabled: boolean };
export type Subscription = { account_type: "free" | "member"; source: "legacy" | "subscription" | "none" | "admin"; expires_at: string | null; server_time: string; purchase_blocked?: boolean };
export type BillingOrder = { id: string; provider: PaymentProvider; amount_fen: number; currency: "CNY"; status: "pending" | "paid" | "closed" | "failed"; qr_code: string | null; expires_at: string; created_at: string; paid_at: string | null; access_ends_at: string | null };
const BASE = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/billing`;

async function read<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    const message = typeof value?.detail === "string" ? value.detail : value?.detail?.message;
    throw new Error(message || (response.status === 503 ? "会员购买暂未开放，请稍后再试。" : "暂时无法读取订阅信息，请重试。"));
  }
  return response.json() as Promise<T>;
}
export const fetchBillingPlans = (signal?: AbortSignal) => fetch(`${BASE}/plans`, { signal, credentials: "same-origin", cache: "no-store" }).then(read<BillingPlans>);
export const fetchSubscription = (signal?: AbortSignal) => privateFetch(`${BASE}/subscription`, { signal }).then(read<Subscription>);
export const fetchBillingOrder = (id: string, signal?: AbortSignal) => privateFetch(`${BASE}/orders/${encodeURIComponent(id)}`, { signal }).then(read<BillingOrder>);
export async function fetchBillingOrders(signal?: AbortSignal): Promise<BillingOrder[]> {
  const value = await privateFetch(`${BASE}/orders`, { signal }).then(read<BillingOrder[] | { items: BillingOrder[] }>);
  return Array.isArray(value) ? value : value.items;
}
export const createBillingOrder = (provider: PaymentProvider, key: string, signal?: AbortSignal) => privateFetch(`${BASE}/orders`, {
  method: "POST", signal, headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify({ provider })
}).then(read<BillingOrder>);
export const simulateBillingPayment = (id: string, signal?: AbortSignal) => privateFetch(`${BASE}/orders/${encodeURIComponent(id)}/simulate-payment`, {
  method: "POST", signal
}).then(read<BillingOrder>);
