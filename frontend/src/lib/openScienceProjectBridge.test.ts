import { describe, expect, it, vi } from "vitest";
import {
  createOpenScienceProjectBridge,
  parseOpenScienceProjectsSnapshot,
  resolveAgentWorkspaceOrigin
} from "./openScienceProjectBridge";

const snapshot = {
  namespace: "openscience.zhijupoly",
  version: 1,
  type: "projects.snapshot",
  projects: [
    {
      directory: "/home/codexlab/DevTool/Alpha",
      name: "Alpha",
      displayPath: "~/DevTool/Alpha",
      updatedAt: 200,
      favorite: true
    }
  ],
  activeDirectory: "/home/codexlab/DevTool/Alpha"
};

describe("resolveAgentWorkspaceOrigin", () => {
  it("只接受 HTTP(S) 工作台地址并返回精确 Origin", () => {
    expect(resolveAgentWorkspaceOrigin("http://127.0.0.1:4454/session")).toBe("http://127.0.0.1:4454");
    expect(resolveAgentWorkspaceOrigin("https://science.example.test/workspace")).toBe(
      "https://science.example.test"
    );
    expect(resolveAgentWorkspaceOrigin("file:///tmp/workspace")).toBeUndefined();
    expect(resolveAgentWorkspaceOrigin("not a url")).toBeUndefined();
  });
});

describe("parseOpenScienceProjectsSnapshot", () => {
  it("接受版本化项目快照", () => {
    expect(parseOpenScienceProjectsSnapshot(snapshot)).toEqual(snapshot);
  });

  it("拒绝错误命名空间、版本和项目字段", () => {
    expect(parseOpenScienceProjectsSnapshot(null)).toBeUndefined();
    expect(parseOpenScienceProjectsSnapshot({ ...snapshot, namespace: "other" })).toBeUndefined();
    expect(parseOpenScienceProjectsSnapshot({ ...snapshot, version: 2 })).toBeUndefined();
    expect(parseOpenScienceProjectsSnapshot({ ...snapshot, activeDirectory: "" })).toBeUndefined();
    expect(
      parseOpenScienceProjectsSnapshot({
        ...snapshot,
        projects: [{ ...snapshot.projects[0], directory: "" }]
      })
    ).toBeUndefined();
  });
});

describe("createOpenScienceProjectBridge", () => {
  it("只接收来自当前 iframe 和工作台 Origin 的项目快照", () => {
    const frameWindow = { postMessage: vi.fn() };
    const onSnapshot = vi.fn();
    const bridge = createOpenScienceProjectBridge({
      workspaceUrl: "http://127.0.0.1:4454",
      getFrameWindow: () => frameWindow,
      onSnapshot
    });

    bridge.handleMessage({ source: {}, origin: "http://127.0.0.1:4454", data: snapshot });
    bridge.handleMessage({ source: frameWindow, origin: "https://other.example.test", data: snapshot });
    bridge.handleMessage({ source: frameWindow, origin: "http://127.0.0.1:4454", data: snapshot });

    expect(onSnapshot).toHaveBeenCalledTimes(1);
    expect(onSnapshot).toHaveBeenCalledWith(snapshot);
  });

  it("使用精确 targetOrigin 请求项目并发送打开命令", () => {
    const postMessage = vi.fn();
    const bridge = createOpenScienceProjectBridge({
      workspaceUrl: "http://127.0.0.1:4454/path",
      getFrameWindow: () => ({ postMessage }),
      onSnapshot: vi.fn()
    });

    expect(bridge.requestProjects()).toBe(true);
    expect(bridge.openProject("/home/codexlab/DevTool/Alpha")).toBe(true);
    expect(postMessage).toHaveBeenNthCalledWith(
      1,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "projects.request"
      },
      "http://127.0.0.1:4454"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      2,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.open",
        directory: "/home/codexlab/DevTool/Alpha"
      },
      "http://127.0.0.1:4454"
    );
  });

  it("iframe 未就绪或工作台 URL 无效时保持静默", () => {
    const invalidBridge = createOpenScienceProjectBridge({
      workspaceUrl: "file:///tmp/workspace",
      getFrameWindow: () => ({ postMessage: vi.fn() }),
      onSnapshot: vi.fn()
    });
    const unloadedBridge = createOpenScienceProjectBridge({
      workspaceUrl: "http://127.0.0.1:4454",
      getFrameWindow: () => null,
      onSnapshot: vi.fn()
    });

    expect(invalidBridge.requestProjects()).toBe(false);
    expect(invalidBridge.openProject("/home/codexlab/DevTool/Alpha")).toBe(false);
    expect(unloadedBridge.requestProjects()).toBe(false);
  });
});
