import { describe, expect, it, vi } from "vitest";
import {
  createOpenScienceGeneralSessionBridge,
  parseOpenScienceGeneralSessionsSnapshot
} from "./openScienceGeneralSessionBridge";

const snapshot = {
  namespace: "openscience.zhijupoly" as const,
  version: 1 as const,
  type: "general.sessions.snapshot" as const,
  sessions: [
    { id: "ses_new", title: "新会话", updatedAt: 200 },
    { id: "ses_old", title: "旧会话", updatedAt: 100 }
  ],
  activeSessionID: "ses_new"
};

describe("parseOpenScienceGeneralSessionsSnapshot", () => {
  it("接受版本化的固定工作区会话快照", () => {
    expect(parseOpenScienceGeneralSessionsSnapshot(snapshot)).toEqual(snapshot);
  });

  it("拒绝错误协议、非法会话字段和不在快照中的活动会话", () => {
    expect(parseOpenScienceGeneralSessionsSnapshot(null)).toBeUndefined();
    expect(
      parseOpenScienceGeneralSessionsSnapshot({ ...snapshot, namespace: "other" })
    ).toBeUndefined();
    expect(parseOpenScienceGeneralSessionsSnapshot({ ...snapshot, version: 2 })).toBeUndefined();
    expect(
      parseOpenScienceGeneralSessionsSnapshot({
        ...snapshot,
        sessions: [{ id: "", title: "坏数据", updatedAt: 1 }]
      })
    ).toBeUndefined();
    expect(
      parseOpenScienceGeneralSessionsSnapshot({ ...snapshot, activeSessionID: "ses_missing" })
    ).toBeUndefined();
  });
});

describe("createOpenScienceGeneralSessionBridge", () => {
  it("只接收来自当前 iframe 和工作台 Origin 的会话快照", () => {
    const frameWindow = { postMessage: vi.fn() };
    const onSnapshot = vi.fn();
    const bridge = createOpenScienceGeneralSessionBridge({
      workspaceUrl: "http://workspace.example.test:9011",
      getFrameWindow: () => frameWindow,
      onSnapshot
    });

    bridge.handleMessage({ data: snapshot, origin: "http://evil.test", source: frameWindow });
    bridge.handleMessage({ data: snapshot, origin: "http://workspace.example.test:9011", source: {} });
    bridge.handleMessage({
      data: snapshot,
      origin: "http://workspace.example.test:9012",
      source: frameWindow
    });
    expect(onSnapshot).not.toHaveBeenCalled();

    bridge.handleMessage({
      data: snapshot,
      origin: "http://workspace.example.test:9011",
      source: frameWindow
    });
    expect(onSnapshot).toHaveBeenCalledWith(snapshot);
  });

  it("请求快照并只向当前快照中的会话发送操作命令", () => {
    const postMessage = vi.fn();
    const frameWindow = { postMessage };
    const bridge = createOpenScienceGeneralSessionBridge({
      workspaceUrl: "http://workspace.example.test:9011/session",
      getFrameWindow: () => frameWindow,
      onSnapshot: vi.fn()
    });

    expect(bridge.requestSessions()).toBe(true);
    expect(bridge.newSession()).toBe(true);
    expect(bridge.openSession("ses_new")).toBe(false);

    bridge.handleMessage({
      data: snapshot,
      origin: "http://workspace.example.test:9011",
      source: frameWindow
    });

    expect(bridge.openSession("ses_new")).toBe(true);
    expect(bridge.renameSession("ses_new", "  新标题  ")).toBe(true);
    expect(bridge.deleteSession("ses_old")).toBe(true);
    expect(bridge.openSession("ses_missing")).toBe(false);
    expect(bridge.renameSession("ses_old", "   ")).toBe(false);
    expect(bridge.deleteSession("ses_missing")).toBe(false);

    expect(postMessage.mock.calls).toEqual([
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.sessions.request"
        },
        "http://workspace.example.test:9011"
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.new"
        },
        "http://workspace.example.test:9011"
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.open",
          sessionID: "ses_new"
        },
        "http://workspace.example.test:9011"
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.rename",
          sessionID: "ses_new",
          title: "新标题"
        },
        "http://workspace.example.test:9011"
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.delete",
          sessionID: "ses_old"
        },
        "http://workspace.example.test:9011"
      ]
    ]);
  });

  it("iframe 未就绪或工作台 URL 无效时保持静默", () => {
    const invalidBridge = createOpenScienceGeneralSessionBridge({
      workspaceUrl: "",
      getFrameWindow: () => ({ postMessage: vi.fn() }),
      onSnapshot: vi.fn()
    });
    const unloadedBridge = createOpenScienceGeneralSessionBridge({
      workspaceUrl: "http://workspace.example.test:9011",
      getFrameWindow: () => null,
      onSnapshot: vi.fn()
    });

    expect(invalidBridge.requestSessions()).toBe(false);
    expect(invalidBridge.newSession()).toBe(false);
    expect(unloadedBridge.requestSessions()).toBe(false);
    expect(unloadedBridge.newSession()).toBe(false);
  });
});
