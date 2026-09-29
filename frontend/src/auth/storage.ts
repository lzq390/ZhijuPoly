import { clearAllTgAssistantImagePreviews } from "../services/tgAssistantImagePreviews";
export const AUTH_SYNC_KEY = "nexpoly.auth.event";
export const AUTH_OWNER_KEY = "nexpoly.auth.owner";
// A server-confirmed owner AND session for origin-shared private storage. This
// is an ownership fence, never a replacement for the authenticated HTTP check.
export const AUTH_SHARED_OWNER_KEY = "nexpoly.auth.shared-owner";
function clearPrivateStorage(name: "localStorage" | "sessionStorage") {
  try {
    const storage = window[name];
    for (const key of Object.keys(storage)) {
      if (/^(nexpoly[.:]|polyprop[._:])/.test(key) && key !== AUTH_SYNC_KEY) storage.removeItem(key);
    }
  } catch { /* Disabled storage cannot retain accessible private state. */ }
}
export function clearPrivateSessionStorage() {
  clearPrivateStorage("sessionStorage");
}
export function clearPrivateWebStorage() {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    clearPrivateStorage(name);
  }
}
export async function clearPrivateBrowserData(clearUrl = true) {
  clearPrivateWebStorage();
  if (clearUrl) window.history.replaceState(null, "", window.location.pathname);
  // Fail closed if accessible IndexedDB cannot be cleared; do not mount a new account.
  await clearAllTgAssistantImagePreviews();
}
