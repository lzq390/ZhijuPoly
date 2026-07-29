import { describe, expect, it, vi } from "vitest";
import {
  createOpenScienceProjectBridge,
  parseOpenScienceProjectsSnapshot,
  resolveAgentWorkspaceOrigin,
  resolveAgentWorkspaceUrl
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
  it("规范化 HTTP(S) 工作台地址并返回精确 Origin", () => {
    expect(resolveAgentWorkspaceUrl("  http://workspace.example.test:9011/session  ")).toBe(
      "http://workspace.example.test:9011/session"
    );
    expect(resolveAgentWorkspaceOrigin("http://workspace.example.test:9011/session")).toBe(
      "http://workspace.example.test:9011"
    );
    expect(resolveAgentWorkspaceOrigin("https://science.example.test/workspace")).toBe(
      "https://science.example.test"
    );
    expect(resolveAgentWorkspaceUrl("")).toBeUndefined();
    expect(resolveAgentWorkspaceUrl("https://user:secret@science.example.test")).toBeUndefined();
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
      workspaceUrl: "http://workspace.example.test:9011",
      getFrameWindow: () => frameWindow,
      onSnapshot
    });

    bridge.handleMessage({ source: {}, origin: "http://workspace.example.test:9011", data: snapshot });
    bridge.handleMessage({ source: frameWindow, origin: "https://other.example.test", data: snapshot });
    bridge.handleMessage({
      source: frameWindow,
      origin: "http://workspace.example.test:9012",
      data: snapshot
    });
    bridge.handleMessage({ source: frameWindow, origin: "http://workspace.example.test:9011", data: snapshot });

    expect(onSnapshot).toHaveBeenCalledTimes(1);
    expect(onSnapshot).toHaveBeenCalledWith(snapshot);
  });

  it("使用精确 targetOrigin 请求项目并发送项目入口命令", () => {
    const postMessage = vi.fn();
    const bridge = createOpenScienceProjectBridge({
      workspaceUrl: "http://workspace.example.test:9011/path",
      getFrameWindow: () => ({ postMessage }),
      onSnapshot: vi.fn()
    });

    expect(bridge.requestProjects()).toBe(true);
    expect(bridge.browseProjects()).toBe(true);
    expect(bridge.newProject()).toBe(true);
    expect(bridge.setProjectFavorite("/home/codexlab/DevTool/Alpha", false)).toBe(true);
    expect(bridge.archiveProject("/home/codexlab/DevTool/Beta")).toBe(true);
    expect(bridge.openProject("/home/codexlab/DevTool/Alpha")).toBe(true);
    expect(postMessage).toHaveBeenNthCalledWith(
      1,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "projects.request"
      },
      "http://workspace.example.test:9011"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      2,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "projects.browse"
      },
      "http://workspace.example.test:9011"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      3,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.new"
      },
      "http://workspace.example.test:9011"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      4,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.favorite.set",
        directory: "/home/codexlab/DevTool/Alpha",
        favorite: false
      },
      "http://workspace.example.test:9011"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      5,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.archive",
        directory: "/home/codexlab/DevTool/Beta"
      },
      "http://workspace.example.test:9011"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      6,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.open",
        directory: "/home/codexlab/DevTool/Alpha"
      },
      "http://workspace.example.test:9011"
    );
  });

  it("iframe 未就绪或工作台 URL 无效时保持静默", () => {
    const invalidBridge = createOpenScienceProjectBridge({
      workspaceUrl: "",
      getFrameWindow: () => ({ postMessage: vi.fn() }),
      onSnapshot: vi.fn()
    });
    const unloadedBridge = createOpenScienceProjectBridge({
      workspaceUrl: "http://workspace.example.test:9011",
      getFrameWindow: () => null,
      onSnapshot: vi.fn()
    });

    expect(invalidBridge.requestProjects()).toBe(false);
    expect(invalidBridge.browseProjects()).toBe(false);
    expect(invalidBridge.newProject()).toBe(false);
    expect(invalidBridge.setProjectFavorite("/home/codexlab/DevTool/Alpha", true)).toBe(false);
    expect(invalidBridge.archiveProject("/home/codexlab/DevTool/Alpha")).toBe(false);
    expect(invalidBridge.openProject("/home/codexlab/DevTool/Alpha")).toBe(false);
    expect(unloadedBridge.requestProjects()).toBe(false);
    expect(unloadedBridge.browseProjects()).toBe(false);
    expect(unloadedBridge.newProject()).toBe(false);
    expect(unloadedBridge.setProjectFavorite("/home/codexlab/DevTool/Alpha", true)).toBe(false);
    expect(unloadedBridge.archiveProject("/home/codexlab/DevTool/Alpha")).toBe(false);
    expect(unloadedBridge.setProjectFavorite("   ", true)).toBe(false);
    expect(unloadedBridge.archiveProject("   ")).toBe(false);
  });
});
