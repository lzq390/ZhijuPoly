/** Only an explicitly configured development entry can forward monitoring reads. */
export function developmentMonitoringProxy(enabled: string | undefined, target: string | undefined, socketPath?: string) {
  if (enabled !== "true") return {};
  const socket = socketPath?.trim();
  if (socket) {
    if (!socket.startsWith("/") || /[\r\n\0]/.test(socket)) throw new Error("Monitoring socket must be an absolute local path");
    return { "/api/v1/server-monitoring": { target: { socketPath: socket, protocol: "http:" }, changeOrigin: false } };
  }
  const destination = target?.trim();
  return destination
    ? { "/api/v1/server-monitoring": { target: destination, changeOrigin: false } }
    : {};
}
