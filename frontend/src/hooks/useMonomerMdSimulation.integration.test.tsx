/* @vitest-environment jsdom */

import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useMonomerMdSimulation } from "./useMonomerMdSimulation";

type ObservedRequest = {
  url: string;
  signal: AbortSignal;
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useMonomerMdSimulation request lifecycle", () => {
  it("aborts both in-flight status fetches when the hook unmounts", async () => {
    const requests: ObservedRequest[] = [];
    const abortedUrls: string[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const signal = init?.signal;
      if (!signal) {
        throw new Error("monomer MD status fetch must receive an AbortSignal");
      }

      const url = String(input);
      requests.push({ url, signal });
      return new Promise<Response>((_resolve, reject) => {
        const rejectAsAborted = () => {
          abortedUrls.push(url);
          reject(new DOMException("The operation was aborted.", "AbortError"));
        };
        if (signal.aborted) {
          rejectAsAborted();
          return;
        }
        signal.addEventListener("abort", rejectAsAborted, { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderHook(() => useMonomerMdSimulation());

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests.map(({ url }) => url).sort()).toEqual([
      "/api/v1/monomer-md/protocols",
      "/api/v1/monomer-md/status"
    ]);
    expect(requests.every(({ signal }) => !signal.aborted)).toBe(true);

    unmount();

    expect(requests.every(({ signal }) => signal.aborted)).toBe(true);
    await waitFor(() =>
      expect(abortedUrls.sort()).toEqual([
        "/api/v1/monomer-md/protocols",
        "/api/v1/monomer-md/status"
      ])
    );
  });
});

vi.mock("./useTaskEvents", () => {
  const rememberJob = vi.fn(), forgetJob = vi.fn(), reconnect = vi.fn();
  return { useTaskEvents: () => ({ connectionState: "live", rememberJob, forgetJob, reconnect }) };
});
