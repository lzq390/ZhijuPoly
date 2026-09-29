import { getSession, sessionIsReady } from "./session";

/** Guest navigation is local; service actions explicitly open the shared login form. */
export function requestServiceAccess(): boolean {
  if (sessionIsReady() && !getSession().authenticated) {
    window.dispatchEvent(new Event("nexpoly:login-required"));
    return false;
  }
  return true;
}
