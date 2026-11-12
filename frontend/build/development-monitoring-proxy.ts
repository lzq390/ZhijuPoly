/** Only an explicitly configured development entry can forward monitoring reads. */
export function developmentMonitoringProxy(enabled: string | undefined, target: string | undefined) {
  const destination = target?.trim();
  return enabled === "true" && destination
    ? { "/api/v1/server-monitoring": { target: destination, changeOrigin: false } }
    : {};
}
