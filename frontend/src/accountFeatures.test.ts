// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { isRegistrationEnabled, isSubscriptionEnabled } from "./accountFeatures";
import { getInitialRoute, pathFromRoute, routeFromPath } from "./routing";
import { openSubscription } from "./auth/access";

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

it.each([undefined, "true", "0", "FALSE"])("preserves account entry defaults for %s", value => {
  vi.stubEnv("VITE_SUBSCRIPTION_ENABLED", value);
  vi.stubEnv("VITE_REGISTRATION_ENABLED", value);
  expect(isSubscriptionEnabled()).toBe(true);
  expect(isRegistrationEnabled()).toBe(true);
  expect(routeFromPath("/account/subscription").module).toBe("subscription");
  expect(pathFromRoute({ module: "subscription", datasetKey: null })).toBe("/account/subscription");
});

it("independently hides each account entry only for explicit false", () => {
  vi.stubEnv("VITE_SUBSCRIPTION_ENABLED", "false");
  expect(isSubscriptionEnabled()).toBe(false);
  expect(isRegistrationEnabled()).toBe(true);
  vi.stubEnv("VITE_SUBSCRIPTION_ENABLED", "true");
  vi.stubEnv("VITE_REGISTRATION_ENABLED", "false");
  expect(isSubscriptionEnabled()).toBe(true);
  expect(isRegistrationEnabled()).toBe(false);
});

it.each(["/account/subscription", "/account/subscription/"])("normalizes disabled %s without affecting personal usage", path => {
  vi.stubEnv("VITE_SUBSCRIPTION_ENABLED", "false");
  expect(routeFromPath(path)).toEqual({ module: "home", datasetKey: null });
  expect(pathFromRoute({ module: "subscription", datasetKey: null })).toBe("/");
  window.history.replaceState({}, "", `${path}?old=order#private`);
  expect(getInitialRoute()).toEqual({ module: "home", datasetKey: null });
  expect(window.location.pathname + window.location.search + window.location.hash).toBe("/");
  expect(routeFromPath("/account/usage").module).toBe("personalUsage");
  expect(pathFromRoute({ module: "personalUsage", datasetKey: null })).toBe("/account/usage");
  const dispatch = vi.spyOn(window, "dispatchEvent");
  openSubscription();
  expect(dispatch).not.toHaveBeenCalled();
});
