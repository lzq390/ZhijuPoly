// Hidden, retained workspaces may contain their own membership notice.
// A visible authentication/loading/error screen must still block readiness.
export function handoffPageReady(doc = document) {
  return doc.fonts.status === "loaded" &&
    [...doc.querySelectorAll(".np-auth-center")].every(element =>
      !element.checkVisibility({ visibilityProperty: true, opacityProperty: true }));
}
