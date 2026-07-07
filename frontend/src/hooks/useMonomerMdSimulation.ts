import { useCallback, useEffect, useRef, useState } from "react";
import { createMonomerMdJob, fetchMonomerMdJob, fetchMonomerMdStatus } from "../services/api";
import type {
  MonomerMdJobResponse,
  MonomerMdJobStatus,
  MonomerMdServiceStatusResponse,
  MonomerMdSimulationResult
} from "../types";

type MonomerMdSimulationState = {
  isLoading: boolean;
  error: string | null;
  data: MonomerMdSimulationResult | null;
  job: MonomerMdJobResponse | null;
  serviceStatus: MonomerMdServiceStatusResponse | null;
  isStatusLoading: boolean;
  statusError: string | null;
};

const POLL_INTERVAL_MS = 1400;
const TERMINAL_STATUSES = new Set<MonomerMdJobStatus>(["completed", "failed", "cancelled"]);

export function getMonomerMdSmilesValidationError(smiles: string): string | null {
  const normalizedSmiles = smiles.trim();
  if (!normalizedSmiles) {
    return "Enter a monomer SMILES.";
  }
  if (normalizedSmiles.includes("*")) {
    return "Monomer MD simulation accepts ordinary SMILES only; remove * repeat-unit markers.";
  }
  return null;
}

export function getMonomerMdJobResult(job: MonomerMdJobResponse | null): MonomerMdSimulationResult | null {
  if (!job) {
    return null;
  }
  if (job.result) {
    return job.result;
  }
  if (job.density_series && job.temperature_series && job.energy_series && job.summary) {
    return {
      density_series: job.density_series,
      temperature_series: job.temperature_series,
      energy_series: job.energy_series,
      trajectory_preview: job.trajectory_preview ?? null,
      summary: job.summary,
      artifacts: job.artifacts ?? []
    };
  }
  return null;
}

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function jobErrorMessage(job: MonomerMdJobResponse) {
  if (job.status === "cancelled") {
    return job.message ?? "Monomer MD simulation was cancelled.";
  }
  return job.error ?? job.message ?? "Monomer MD simulation failed.";
}

export function useMonomerMdSimulation() {
  const [smiles, setSmiles] = useState("");
  const [state, setState] = useState<MonomerMdSimulationState>({
    isLoading: false,
    error: null,
    data: null,
    job: null,
    serviceStatus: null,
    isStatusLoading: false,
    statusError: null
  });
  const pollTokenRef = useRef(0);
  const statusTokenRef = useRef(0);

  const refreshStatus = useCallback(async () => {
    const token = statusTokenRef.current + 1;
    statusTokenRef.current = token;
    setState((current) => ({ ...current, isStatusLoading: true, statusError: null }));
    try {
      const serviceStatus = await fetchMonomerMdStatus();
      if (statusTokenRef.current !== token) {
        return;
      }
      setState((current) => ({ ...current, serviceStatus, isStatusLoading: false, statusError: null }));
    } catch (error) {
      if (statusTokenRef.current !== token) {
        return;
      }
      setState((current) => ({
        ...current,
        serviceStatus: null,
        isStatusLoading: false,
        statusError: error instanceof Error ? error.message : "Failed to check monomer MD service status."
      }));
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    return () => {
      pollTokenRef.current += 1;
      statusTokenRef.current += 1;
    };
  }, [refreshStatus]);

  async function pollJob(jobId: string, token: number) {
    while (pollTokenRef.current === token) {
      const job = await fetchMonomerMdJob(jobId);
      if (pollTokenRef.current !== token) {
        return;
      }
      const result = getMonomerMdJobResult(job);
      const isTerminal = TERMINAL_STATUSES.has(job.status);
      setState((current) => ({
        ...current,
        isLoading: !isTerminal,
        error: job.status === "failed" || job.status === "cancelled" ? jobErrorMessage(job) : null,
        data: result ?? current.data,
        job
      }));
      if (isTerminal) {
        return;
      }
      await delay(POLL_INTERVAL_MS);
    }
  }

  async function submit(nextSmiles = smiles) {
    const normalizedSmiles = nextSmiles.trim();
    const validationError = getMonomerMdSmilesValidationError(normalizedSmiles);
    if (validationError) {
      setState((current) => ({ ...current, isLoading: false, error: validationError }));
      return;
    }
    const token = pollTokenRef.current + 1;
    pollTokenRef.current = token;
    setSmiles(normalizedSmiles);
    setState((current) => ({ ...current, isLoading: true, error: null, data: null, job: null }));
    try {
      const createdJob = await createMonomerMdJob({ smiles: normalizedSmiles });
      if (pollTokenRef.current !== token) {
        return;
      }
      setState((current) => ({
        ...current,
        job: { job_id: createdJob.job_id, status: createdJob.status, smiles: normalizedSmiles }
      }));
      await pollJob(createdJob.job_id, token);
    } catch (error) {
      if (pollTokenRef.current !== token) {
        return;
      }
      setState((current) => ({
        ...current,
        isLoading: false,
        error: error instanceof Error ? error.message : "Failed to submit monomer MD simulation."
      }));
    }
  }

  function reset() {
    pollTokenRef.current += 1;
    setState((current) => ({ ...current, isLoading: false, error: null, data: null, job: null }));
  }

  return {
    smiles,
    setSmiles,
    ...state,
    submit,
    reset,
    refreshStatus
  };
}
