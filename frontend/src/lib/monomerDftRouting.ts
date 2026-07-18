const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function getMonomerDftJobIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("job")?.trim() ?? "";
  return UUID_PATTERN.test(value) ? value : null;
}

export function hasInvalidMonomerDftJobSearch(search: string): boolean {
  const params = new URLSearchParams(search);
  return params.has("job") && getMonomerDftJobIdFromSearch(search) == null;
}

export function getMonomerDftPath(jobId: string | null): string {
  return jobId ? `/monomer-dft?${new URLSearchParams({ job: jobId }).toString()}` : "/monomer-dft";
}
