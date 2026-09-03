const MONOMER_MD_JOB_ID_PATTERN = /^[0-9a-f]{32}$/i;

export function isMonomerMdJobId(value: string): boolean {
  return MONOMER_MD_JOB_ID_PATTERN.test(value);
}

export function getMonomerMdJobIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("job")?.trim() ?? "";
  return isMonomerMdJobId(value) ? value : null;
}

export function hasInvalidMonomerMdJobSearch(search: string): boolean {
  const params = new URLSearchParams(search);
  return params.has("job") && getMonomerMdJobIdFromSearch(search) == null;
}

export function getMonomerMdPath(jobId: string | null): string {
  return jobId
    ? `/monomer-md-simulation?${new URLSearchParams({ job: jobId }).toString()}`
    : "/monomer-md-simulation";
}
