/** Display policy only; account permissions and server checks remain authoritative. */
export function isSubscriptionEnabled() {
  return import.meta.env.VITE_SUBSCRIPTION_ENABLED !== "false";
}

export function isRegistrationEnabled() {
  return import.meta.env.VITE_REGISTRATION_ENABLED !== "false";
}
