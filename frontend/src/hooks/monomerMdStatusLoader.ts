import type { MonomerMdProtocolCatalogResponse, MonomerMdServiceStatusResponse } from "../types";

export const MONOMER_MD_STATUS_TIMEOUT_MS = 10_000;

type StatusFetcher = (signal: AbortSignal) => Promise<MonomerMdServiceStatusResponse>;
type ProtocolsFetcher = (signal: AbortSignal) => Promise<MonomerMdProtocolCatalogResponse>;

export type MonomerMdStatusLoadResult = {
  status: PromiseSettledResult<MonomerMdServiceStatusResponse>;
  protocols: PromiseSettledResult<MonomerMdProtocolCatalogResponse>;
  timedOut: boolean;
};

type ActiveRequest = {
  controller: AbortController;
  timeoutId: ReturnType<typeof setTimeout> | null;
  timedOut: boolean;
};

export class MonomerMdStatusLoader {
  private generation = 0;
  private activeRequest: ActiveRequest | null = null;

  constructor(
    private readonly fetchStatus: StatusFetcher,
    private readonly fetchProtocols: ProtocolsFetcher,
    private readonly timeoutMs = MONOMER_MD_STATUS_TIMEOUT_MS
  ) {}

  async load(): Promise<MonomerMdStatusLoadResult | null> {
    const generation = this.generation + 1;
    this.generation = generation;
    this.abortActiveRequest();

    const controller = new AbortController();
    const activeRequest: ActiveRequest = { controller, timeoutId: null, timedOut: false };
    this.activeRequest = activeRequest;
    activeRequest.timeoutId = globalThis.setTimeout(() => {
      if (this.activeRequest !== activeRequest) {
        return;
      }
      activeRequest.timedOut = true;
      controller.abort();
    }, this.timeoutMs);

    const statusPromise = Promise.resolve().then(() => this.fetchStatus(controller.signal));
    const protocolsPromise = Promise.resolve().then(() => this.fetchProtocols(controller.signal));
    const [status, protocols] = await Promise.allSettled([statusPromise, protocolsPromise]);

    this.clearRequestTimeout(activeRequest);
    if (this.activeRequest === activeRequest) {
      this.activeRequest = null;
    }
    if (this.generation !== generation) {
      return null;
    }

    return { status, protocols, timedOut: activeRequest.timedOut };
  }

  cancel(): void {
    this.generation += 1;
    this.abortActiveRequest();
  }

  private abortActiveRequest(): void {
    const activeRequest = this.activeRequest;
    this.activeRequest = null;
    if (!activeRequest) {
      return;
    }
    this.clearRequestTimeout(activeRequest);
    if (!activeRequest.controller.signal.aborted) {
      activeRequest.controller.abort();
    }
  }

  private clearRequestTimeout(activeRequest: ActiveRequest): void {
    if (activeRequest.timeoutId === null) {
      return;
    }
    globalThis.clearTimeout(activeRequest.timeoutId);
    activeRequest.timeoutId = null;
  }
}

export function monomerMdStatusLoadError(
  result: PromiseSettledResult<unknown>,
  timedOut: boolean,
  fallbackMessage: string,
  timeoutMessage: string
): string | null {
  if (result.status === "fulfilled") {
    return null;
  }
  if (timedOut && isAbortError(result.reason)) {
    return timeoutMessage;
  }
  return result.reason instanceof Error ? result.reason.message : fallbackMessage;
}

function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "name" in error && error.name === "AbortError";
}
