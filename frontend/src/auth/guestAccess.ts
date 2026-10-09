import { getSession, sessionIsReady } from "./session";

/** Guest navigation is local; service actions explicitly open the shared login form. */
export function requestServiceAccess(): boolean {
  if (sessionIsReady() && !getSession().authenticated) {
    window.dispatchEvent(new Event("nexpoly:login-required"));
    return false;
  }
  if (sessionIsReady() && getSession().account_type === "free") {
    window.dispatchEvent(new Event("nexpoly:membership-required"));
    return false;
  }
  return true;
}
