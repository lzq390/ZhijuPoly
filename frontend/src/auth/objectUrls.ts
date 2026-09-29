import { assertSessionEpoch, getSessionEpoch, onSessionRetired } from "./session";

const urls = new Set<string>();

export function createPrivateObjectURL(blob: Blob, expectedEpoch = getSessionEpoch()): string {
  assertSessionEpoch(expectedEpoch);
  const url = URL.createObjectURL(blob);
  urls.add(url);
  return url;
}

export function revokePrivateObjectURL(url: string) {
  urls.delete(url);
  URL.revokeObjectURL(url);
}

onSessionRetired(() => {
  for (const url of urls) {
    try { URL.revokeObjectURL(url); } catch { /* Continue retiring every preview. */ }
  }
  urls.clear();
});
