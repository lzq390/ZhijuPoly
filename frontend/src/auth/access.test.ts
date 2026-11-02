import { expect, it } from "vitest";
import { isLocalOnly } from "./access";

it("keeps ordinary authenticated accounts on the same service boundary as guests", () => {
  expect(isLocalOnly({ status: "guest" })).toBe(true);
  expect(isLocalOnly({ status: "authenticated", session: { account_type: "free" } })).toBe(true);
  expect(isLocalOnly({ status: "authenticated", session: { account_type: "member" } })).toBe(false);
  // Older session payloads remain compatible with their existing member behavior.
  expect(isLocalOnly({ status: "authenticated", session: {} })).toBe(false);
});
