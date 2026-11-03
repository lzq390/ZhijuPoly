import { SIMULATION_PAYMENT_PATH } from "../../shared/billing-routes";
import type { PaymentProvider } from "./billing";

export type SimulationCheckout = {
  simulation: true; provider: PaymentProvider; amount_fen: number; currency: "CNY"; period: "month";
  status: "pending" | "paid" | "closed"; order_suffix: string; expires_at: string; server_time: string; access_ends_at: string | null;
};
export class SimulationCheckoutError extends Error {
  constructor(public readonly kind: "invalid" | "expired" | "closed" | "unavailable") { super(kind); }
}
const BASE = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/billing/simulation`;
const TOKEN_PATTERN = /^[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
export function simulationTokenFromHash(hash: string): string | null {
  const values = new URLSearchParams(hash.replace(/^#/, "")).getAll("token");
  return values.length === 1 && TOKEN_PATTERN.test(values[0]) ? values[0] : null;
}
export function simulationCheckoutUrl(token: string, origin: string): string | null {
  if (!TOKEN_PATTERN.test(token)) return null;
  const url = new URL(SIMULATION_PAYMENT_PATH, origin);
  url.hash = `token=${encodeURIComponent(token)}`;
  return url.href;
}
const validDate = (value: unknown) => typeof value === "string" && Number.isFinite(Date.parse(value));
async function request(action: "checkout" | "confirm", token: string, signal?: AbortSignal): Promise<SimulationCheckout> {
  const response = await fetch(`${BASE}/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
    signal, credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store"
  });
  if (!response.ok) throw new SimulationCheckoutError(response.status === 404 ? "invalid" : response.status === 410 ? "expired" : response.status === 409 ? "closed" : "unavailable");
  const value = await response.json();
  if (value?.simulation !== true || !["wechat", "alipay"].includes(value.provider) || value.currency !== "CNY" || value.period !== "month"
    || !Number.isSafeInteger(value.amount_fen) || value.amount_fen <= 0 || !["pending", "paid", "closed"].includes(value.status)
    || typeof value.order_suffix !== "string" || !/^[a-f0-9]{6}$/i.test(value.order_suffix)
    || !validDate(value.expires_at) || !validDate(value.server_time) || (value.access_ends_at !== null && !validDate(value.access_ends_at))) {
    throw new SimulationCheckoutError("unavailable");
  }
  return value as SimulationCheckout;
}
export const readSimulationCheckout = (token: string, signal?: AbortSignal) => request("checkout", token, signal);
export const confirmSimulationCheckout = (token: string, signal?: AbortSignal) => request("confirm", token, signal);
