// Browser-only fixtures. Requests are intercepted; no real task is submitted.
const dftCapabilities = {
  enabled: true, available: true, schema_ready: true,
  calculation_types: ["single_point", "optimization"],
  properties: ["energy", "forces", "charges", "hessian", "frequencies"],
  default_model: "aimnet2",
  models: [{ id: "aimnet2", label: "AIMNet2", description: "Local verification fixture", available: true,
    supported_calculation_types: ["single_point", "optimization"],
    supported_properties: ["energy", "forces", "charges", "hessian", "frequencies"],
    supported_elements: ["H", "C", "N", "O"], supports_spin: false, charge_min: -5, charge_max: 5 }],
  defaults: { conformer: { seed: 1, max_iterations: 500 },
    single_point: { properties: ["energy", "forces", "charges"] },
    optimization: { fmax_eV_per_A: 0.01, max_steps: 50, post_optimization_properties: [] } },
  limits: { max_optimization_steps: 50, min_optimization_steps: 10, max_concurrent_jobs: 1, max_queued_jobs: 8, max_active_jobs: 9 }
};

export function taskThemeFixture(path) {
  const dft = path === "/monomer-dft";
  const roles = [[dft ? "queued" : "submitted", "neutral"], ["running", "info"],
    ["cancel_requested", "warning"], ["completed", "success"], ["failed", "danger"], ["cancelled", "neutral"]];
  const jobs = roles.map(([status]) => ({
    job_id: `page-review-${status}`, status, calculation_type: "single_point", protocol: "Density", run_mode: "formal",
    request: { calculation_type: "single_point", input: { smiles: "CCO" }, model: "aimnet2" },
    progress_percent: status === "completed" ? 100 : 0, queue_position: null,
    created_at: "2026-09-14T00:00:00Z", updated_at: "2026-09-14T00:00:00Z"
  }));
  return {
    roles, selector: dft ? ".np-dft-status-badge" : ".np-mmd-status-pill",
    response(url) {
      if (/\/monomer-(dft|md)\/jobs$/.test(url.pathname)) {
        const items = url.searchParams.get("active_only") === "true"
          ? jobs.filter(job => !["completed", "failed", "cancelled"].includes(job.status)) : jobs;
        return { items, page: 1, page_size: 10, total: items.length };
      }
      if (url.pathname.endsWith("/monomer-dft/capabilities")) return dftCapabilities;
      if (url.pathname.endsWith("/monomer-dft/status")) return {
        enabled: true, available: true, schema_ready: true, worker_status: "ready", runtime_ready: true,
        draining: false, active_jobs: 0, max_active_jobs: 9, message: "Local verification fixture"
      };
      if (url.pathname.endsWith("/monomer-md/status")) return {
        enabled: true, available: true, can_submit: true, default_steps: 300, formal_can_submit: true,
        formal_running_jobs: 1, formal_queued_jobs: 1, formal_max_running_jobs: 1, formal_max_queued_jobs: 8
      };
      if (url.pathname.endsWith("/monomer-md/protocols")) return {
        enabled: true, available: true, protocols: [{ protocol: "Density", run_mode: "formal", runtime_ready: true }],
        message: "Local verification fixture"
      };
    }
  };
}
