import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MonomerMdProtocolCatalogResponse,
  MonomerMdServiceStatusResponse
} from "../types";
import { useMonomerMdSimulation } from "./useMonomerMdSimulation";

const api = vi.hoisted(() => ({
  cancelMonomerMdJob: vi.fn(),
  createMonomerMdJob: vi.fn(),
  deleteMonomerMdArtifacts: vi.fn(),
  fetchMonomerMdJob: vi.fn(),
  fetchMonomerMdJobs: vi.fn(),
  fetchMonomerMdProtocols: vi.fn(),
  fetchMonomerMdStatus: vi.fn()
}));

vi.mock("../services/api", () => api);

const protocolCatalog: MonomerMdProtocolCatalogResponse = {
  enabled: true,
  available: true,
  protocols: [],
  message: "ready"
};
const busyStatus: MonomerMdServiceStatusResponse = {
  enabled: true,
  available: true,
  busy: true,
  draining: false,
  can_submit: false
};
const drainingStatus: MonomerMdServiceStatusResponse = {
  enabled: true,
  available: true,
  busy: false,
  draining: true,
  can_submit: false
};
const readyStatus: MonomerMdServiceStatusResponse = {
  enabled: true,
  available: true,
  busy: false,
  draining: false,
  can_submit: true
};

type MonomerMdSimulationHook = ReturnType<typeof useMonomerMdSimulation>;

let hook: MonomerMdSimulationHook | null = null;
let clock = 0;
let nextTimerId = 1;
const timers = new Map<number, { callback: () => void; runAt: number }>();

function Harness() {
  hook = useMonomerMdSimulation();
  return null;
}

function currentHook(): MonomerMdSimulationHook {
  if (hook === null) {
    throw new Error("Monomer MD hook has not rendered");
  }
  return hook;
}

async function advanceTime(ms: number) {
  clock += ms;
  await act(async () => {
    while (true) {
      const nextTimer = [...timers.entries()]
        .filter(([, timer]) => timer.runAt <= clock)
        .sort((left, right) => left[1].runAt - right[1].runAt)[0];
      if (!nextTimer) {
        break;
      }
      timers.delete(nextTimer[0]);
      nextTimer[1].callback();
      await Promise.resolve();
      await Promise.resolve();
    }
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  hook = null;
  clock = 0;
  nextTimerId = 1;
  timers.clear();
  api.fetchMonomerMdProtocols.mockResolvedValue(protocolCatalog);
  api.fetchMonomerMdJobs.mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 20
  });
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("window", {
    setTimeout: (callback: () => void, ms: number) => {
      const timerId = nextTimerId;
      nextTimerId += 1;
      timers.set(timerId, { callback, runAt: clock + ms });
      return timerId;
    },
    clearTimeout: (timerId: number) => {
      timers.delete(timerId);
    }
  });
});

afterEach(() => {
  timers.clear();
  vi.unstubAllGlobals();
});

describe("Monomer MD service status polling", () => {
  it("refreshes a busy service after five seconds and stops when capacity recovers", async () => {
    api.fetchMonomerMdStatus
      .mockResolvedValueOnce(busyStatus)
      .mockResolvedValueOnce(readyStatus);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });

    expect(api.fetchMonomerMdStatus).toHaveBeenCalledOnce();
    expect(currentHook().serviceStatus).toEqual(busyStatus);
    expect(timers.size).toBe(1);

    await advanceTime(4_999);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledOnce();

    await advanceTime(1);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledTimes(2);
    expect(currentHook().serviceStatus).toEqual(readyStatus);
    expect(timers.size).toBe(0);

    await advanceTime(10_000);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledTimes(2);

    act(() => renderer!.unmount());
  });

  it("keeps polling a draining service across a transient refresh failure", async () => {
    api.fetchMonomerMdStatus
      .mockResolvedValueOnce(drainingStatus)
      .mockRejectedValueOnce(new Error("temporary status failure"))
      .mockResolvedValueOnce(readyStatus);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });

    await advanceTime(5_000);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledTimes(2);
    expect(currentHook().serviceStatus).toEqual(drainingStatus);
    expect(currentHook().statusError).toBe("temporary status failure");
    expect(timers.size).toBe(1);

    await advanceTime(5_000);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledTimes(3);
    expect(currentHook().serviceStatus).toEqual(readyStatus);
    expect(currentHook().statusError).toBeNull();
    expect(timers.size).toBe(0);

    act(() => renderer!.unmount());
  });

  it("does not schedule another refresh after unmounting during a request", async () => {
    let resolveStatus: ((status: MonomerMdServiceStatusResponse) => void) | null = null;
    api.fetchMonomerMdStatus
      .mockResolvedValueOnce(busyStatus)
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveStatus = resolve;
      }));

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await advanceTime(5_000);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledTimes(2);
    expect(timers.size).toBe(0);

    act(() => renderer!.unmount());
    await act(async () => {
      resolveStatus?.(busyStatus);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(timers.size).toBe(0);

    await advanceTime(10_000);
    expect(api.fetchMonomerMdStatus).toHaveBeenCalledTimes(2);
  });
});

describe("Monomer MD task response isolation", () => {
  it("does not reselect a cancelled task after the user switches tasks", async () => {
    let resolveCancellation:
      | ((job: {
          job_id: string;
          status: "cancel_requested";
          run_mode: "formal";
        }) => void)
      | null = null;
    api.fetchMonomerMdStatus.mockResolvedValue(readyStatus);
    api.fetchMonomerMdJob.mockImplementation((jobId: string) =>
      Promise.resolve(
        jobId === "formal-a"
          ? { job_id: jobId, status: "submitted", run_mode: "formal" }
          : { job_id: jobId, status: "completed", run_mode: "formal" }
      )
    );
    api.cancelMonomerMdJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCancellation = resolve;
        })
    );

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await currentHook().loadJob("formal-a");
    });
    expect(currentHook().job?.job_id).toBe("formal-a");

    let cancellation: Promise<void>;
    await act(async () => {
      cancellation = currentHook().cancelJob(currentHook().job!);
      await Promise.resolve();
    });
    await act(async () => {
      await currentHook().loadJob("formal-b");
    });
    expect(currentHook().job?.job_id).toBe("formal-b");

    await act(async () => {
      resolveCancellation?.({
        job_id: "formal-a",
        status: "cancel_requested",
        run_mode: "formal"
      });
      await cancellation!;
    });

    expect(currentHook().job?.job_id).toBe("formal-b");
    expect(api.fetchMonomerMdJob).not.toHaveBeenLastCalledWith(
      "formal-a",
      expect.anything()
    );
    act(() => renderer!.unmount());
  });
});
