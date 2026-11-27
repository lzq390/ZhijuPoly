// Adapt only the entry actually referenced by the reviewed November UI index.
import { createHash } from "node:crypto";

const entry = "/assets/index-production-52b20bca83f6.js";
const digest = "52b20bca83f65772bb46e040d7252873ed90115c2910402abd11ed49d135a2f4";
const anchor = 'const trustedParent=XA(document.referrer);return trustedParent&&["http://114.214.255.154:9000","http://114.214.255.154:9001"].includes(trustedParent)';

export function patchUiAsset(pathname, originalText, parentOrigins) {
  if (pathname !== entry && "/" + pathname !== entry) return originalText;
  if (createHash("sha256").update(originalText).digest("hex") !== digest ||
      originalText.split(anchor).length !== 2) throw Error("Unexpected OpenScience UI build");
  if (!Array.isArray(parentOrigins) || !parentOrigins.length ||
      !parentOrigins.every(origin => new URL(origin).origin === origin && /^https?:/.test(origin))) {
    throw Error("Explicit trusted parent origins required");
  }
  return originalText.replace(anchor, "const trustedParent=XA(history.state?.npParent || document.referrer);return trustedParent&&" +
    JSON.stringify(parentOrigins) + ".includes(trustedParent)");
}
