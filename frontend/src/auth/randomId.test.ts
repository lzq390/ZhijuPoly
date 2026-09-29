import { afterEach, expect, it, vi } from "vitest";
import { randomId } from "./randomId";

afterEach(() => vi.unstubAllGlobals());
it("produces RFC 4122 version 4 IDs without the secure-context randomUUID API", () => {
  vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => { bytes.fill(0xff); return bytes; } });
  expect(randomId()).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
});
it("obtains fresh cryptographic randomness for every ID", () => {
  const first = randomId(), second = randomId();
  expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(second).not.toBe(first);
});
