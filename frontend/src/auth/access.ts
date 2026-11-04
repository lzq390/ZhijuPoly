import { isSubscriptionEnabled } from "../accountFeatures";

/** Authentication and permission to run member services are separate states. */
export function isLocalOnly(auth: { status: string; session?: { account_type?: string } } | null | undefined) {
  return auth?.status === "guest" || auth?.session?.account_type === "free";
}

export function openSubscription() {
  if (!isSubscriptionEnabled()) return;
  window.dispatchEvent(new Event("nexpoly:open-subscription"));
}
