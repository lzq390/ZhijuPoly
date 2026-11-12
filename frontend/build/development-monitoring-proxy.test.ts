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
