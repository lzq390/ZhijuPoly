import { expect, it } from "vitest";
import { developmentMonitoringProxy } from "./development-monitoring-proxy";

it("only installs the monitoring proxy with an explicit enabled entry and target", () => {
  expect(developmentMonitoringProxy(undefined, undefined)).toEqual({});
  expect(developmentMonitoringProxy("true", undefined)).toEqual({});
  expect(developmentMonitoringProxy("false", "http://127.0.0.1:5319")).toEqual({});
  const proxy = { ...developmentMonitoringProxy("true", "http://127.0.0.1:5319"), "/api": { target: "http://127.0.0.1:18000", changeOrigin: false } };
  expect(Object.keys(proxy)).toEqual(["/api/v1/server-monitoring", "/api"]);
  expect(proxy["/api/v1/server-monitoring"]).toEqual({ target: "http://127.0.0.1:5319", changeOrigin: false });
});

it("uses a private Unix socket for the container entry while keeping the browser Host", () => {
  expect(developmentMonitoringProxy("true", undefined, "/app/server-monitoring/api.sock")).toEqual({
    "/api/v1/server-monitoring": { target: { socketPath: "/app/server-monitoring/api.sock", protocol: "http:" }, changeOrigin: false }
  });
  expect(developmentMonitoringProxy("false", undefined, "/app/server-monitoring/api.sock")).toEqual({});
  expect(() => developmentMonitoringProxy("true", undefined, "relative.sock")).toThrow();
  expect(() => developmentMonitoringProxy("true", undefined, "/tmp/socket\nname")).toThrow();
});
