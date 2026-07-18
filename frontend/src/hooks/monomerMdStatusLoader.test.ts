import { afterEach, describe, expect, it, vi } from "vitest";
import type { MonomerMdProtocolCatalogResponse, MonomerMdServiceStatusResponse } from "../types";
import { MonomerMdStatusLoader, monomerMdStatusLoadError } from "./monomerMdStatusLoader";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

const serviceStatus: MonomerMdServiceStatusResponse = { available: true, status: "ready" };
const protocolCatalog: MonomerMdProtocolCatalogResponse = {
  enabled: true,
  available: true,
  protocols: [],
  message: "ready"
};

afterEach(() => {
  vi.useRealTimers();
});

describe("MonomerMdStatusLoader", () => {
  it("starts status and protocols concurrently with one shared abort signal", async () => {
    const statusRequest = deferred<MonomerMdServiceStatusResponse>();
    const protocolsRequest = deferred<MonomerMdProtocolCatalogResponse>();
    const calls: Array<{ endpoint: string; signal: AbortSignal }> = [];
    const loader = new MonomerMdStatusLoader(
      (signal) => {
        calls.push({ endpoint: "status", signal });
        return statusRequest.promise;
      },
      (signal) => {
        calls.push({ endpoint: "protocols", signal });
        return protocolsRequest.promise;
      }
    );

    const load = loader.load();
    await Promise.resolve();

    expect(calls.map(({ endpoint }) => endpoint)).toEqual(["status", "protocols"]);
    expect(calls[0].signal).toBe(calls[1].signal);

    statusRequest.resolve(serviceStatus);
    protocolsRequest.resolve(protocolCatalog);
    const result = await load;

    expect(result?.status).toEqual({ status: "fulfilled", value: serviceStatus });
    expect(result?.protocols).toEqual({ status: "fulfilled", value: protocolCatalog });
  });

  it("retains each endpoint's success or error independently", async () => {
    const protocolsError = new Error("protocol catalog unavailable");
    const loader = new MonomerMdStatusLoader(
      async () => serviceStatus,
      async () => {
        throw protocolsError;
      }
    );

    const result = await loader.load();

    expect(result?.status).toEqual({ status: "fulfilled", value: serviceStatus });
    expect(result?.protocols).toEqual({ status: "rejected", reason: protocolsError });
    expect(monomerMdStatusLoadError(result!.status, false, "status failed", "status timed out")).toBeNull();
    expect(monomerMdStatusLoadError(result!.protocols, false, "protocols failed", "protocols timed out")).toBe(
      protocolsError.message
    );
  });

  it("aborts both requests and completes after the shared 10 second timeout", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const signals: AbortSignal[] = [];
    const waitForAbort = (signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<never>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    };
    const loader = new MonomerMdStatusLoader(waitForAbort, waitForAbort);

    const load = loader.load();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(9_999);
    expect(signals[0].aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    const result = await load;

    expect(signals).toHaveLength(2);
    expect(signals[0]).toBe(signals[1]);
    expect(signals[0].aborted).toBe(true);
    expect(result?.timedOut).toBe(true);
    expect(result?.status.status).toBe("rejected");
    expect(result?.protocols.status).toBe("rejected");
    expect(monomerMdStatusLoadError(result!.status, true, "status failed", "status timed out")).toBe(
      "status timed out"
    );
  });

  it("cancels the real requests when the owner unmounts", async () => {
    const signals: AbortSignal[] = [];
    const waitForAbort = (signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<never>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    };
    const loader = new MonomerMdStatusLoader(waitForAbort, waitForAbort);

    const load = loader.load();
    await Promise.resolve();
    loader.cancel();

    expect(signals).toHaveLength(2);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    await expect(load).resolves.toBeNull();
  });

  it("discards an older response after a newer refresh starts", async () => {
    const statusRequests = [deferred<MonomerMdServiceStatusResponse>(), deferred<MonomerMdServiceStatusResponse>()];
    const protocolRequests = [
      deferred<MonomerMdProtocolCatalogResponse>(),
      deferred<MonomerMdProtocolCatalogResponse>()
    ];
    const statusSignals: AbortSignal[] = [];
    const protocolSignals: AbortSignal[] = [];
    let statusIndex = 0;
    let protocolIndex = 0;
    const loader = new MonomerMdStatusLoader(
      (signal) => {
        statusSignals.push(signal);
        return statusRequests[statusIndex++].promise;
      },
      (signal) => {
        protocolSignals.push(signal);
        return protocolRequests[protocolIndex++].promise;
      }
    );

    const olderLoad = loader.load();
    await Promise.resolve();
    const newerLoad = loader.load();
    await Promise.resolve();

    expect(statusSignals[0].aborted).toBe(true);
    expect(protocolSignals[0].aborted).toBe(true);

    const newerStatus = { ...serviceStatus, worker_status: "newer" };
    const newerProtocols = { ...protocolCatalog, message: "newer" };
    statusRequests[1].resolve(newerStatus);
    protocolRequests[1].resolve(newerProtocols);
    const newerResult = await newerLoad;
    expect(newerResult?.status).toEqual({ status: "fulfilled", value: newerStatus });
    expect(newerResult?.protocols).toEqual({ status: "fulfilled", value: newerProtocols });

    statusRequests[0].resolve({ ...serviceStatus, worker_status: "older" });
    protocolRequests[0].resolve({ ...protocolCatalog, message: "older" });
    await expect(olderLoad).resolves.toBeNull();
  });
});
