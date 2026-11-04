/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AgentWorkspaceHomePage } from "./AgentWorkspaceHomePage";
import type { AuthSession } from "../auth/session";
const mocks = vi.hoisted(() => ({ access: vi.fn(() => false), auth: { status: "guest", session: undefined as AuthSession | undefined } }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => mocks.auth }));
vi.mock("../auth/guestAccess", () => ({ requestServiceAccess: mocks.access }));
beforeEach(() => { mocks.access.mockClear(); mocks.auth = { status: "guest", session: undefined }; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
it("keeps guest suggestions local and never creates the configured iframe", () => {
 const view=render(<AgentWorkspaceHomePage src="http://127.0.0.1:9/synthetic" />);
 fireEvent.click(screen.getByRole("button", {name: /文献与知识/}));
 expect((screen.getByRole("textbox", {name:"发送消息"}) as HTMLTextAreaElement).value).toContain("聚酰亚胺");
 expect(mocks.access).not.toHaveBeenCalled(); expect(view.container.querySelector("iframe")).toBeNull();
});
it("supports roving tabs and keeps the local draft when switching views", () => {
 render(<AgentWorkspaceHomePage />); const chat=screen.getByRole("tab", {name:"聊天"});
 fireEvent.change(screen.getByRole("textbox", {name:"发送消息"}), {target:{value:"Synthetic local draft"}});
 fireEvent.keyDown(chat,{key:"ArrowRight"}); const skills=screen.getByRole("tab", {name:"技能"});
 expect(skills.getAttribute("aria-selected")).toBe("true"); expect(document.activeElement).toBe(skills);
 expect(screen.queryByRole("textbox")).toBeNull(); expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
 fireEvent.keyDown(skills,{key:"Home"}); expect(document.activeElement).toBe(chat);
 expect((screen.getByRole("textbox", {name:"发送消息"}) as HTMLTextAreaElement).value).toBe("Synthetic local draft");
});
it("routes guest send and skill actions through the existing login gate", () => {
 const view=render(<AgentWorkspaceHomePage />);
 fireEvent.submit(screen.getByRole("textbox",{name:"发送消息"}).closest("form")!);
 expect(mocks.access).toHaveBeenCalledTimes(1); fireEvent.click(screen.getByRole("tab", {name:"技能"}));
 fireEvent.click(screen.getByRole("button", {name:"登录后使用技能"})); expect(mocks.access).toHaveBeenCalledTimes(2);
 expect(view.container.querySelector("iframe")).toBeNull();
});

function authenticate(accountType: "free" | "member", userId = "owner", sessionId = "same-session") {
 mocks.auth = { status: "authenticated", session: {
  authenticated: true, user: { id: userId, username: userId, must_change_password: false },
  session_id: sessionId, csrf_token: "test-csrf", capabilities: {}, account_type: accountType
 } };
}

it("keeps free-account access checks without subscription promotion when it is hidden", () => {
 vi.stubEnv("VITE_SUBSCRIPTION_ENABLED", "false"); authenticate("free");
 render(<AgentWorkspaceHomePage />);
 expect(screen.getByText("当前账号暂无发送消息权限")).toBeTruthy();
 expect(screen.queryByText(/开通会员/)).toBeNull();
 fireEvent.click(screen.getByRole("tab", { name: "技能" }));
 expect(screen.getByText("当前账号暂无智能体技能权限，可继续浏览科研模块。")).toBeTruthy();
 fireEvent.click(screen.getByRole("button", { name: "查看功能权限" }));
 expect(mocks.access).toHaveBeenCalledOnce();
 expect(document.querySelector("iframe")).toBeNull();
});

it("preserves an unsent question through same-account membership changes without sending it", async () => {
 const writeText = vi.fn().mockResolvedValue(undefined);
 vi.stubGlobal("navigator", { clipboard: { writeText } });
 authenticate("free");
 const view = render(<AgentWorkspaceHomePage src="https://workspace.example.test/synthetic" />);
 const draft = "Synthetic unsent research question\nwith a second line";
 fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), { target: { value: draft } });
 authenticate("member");
 view.rerender(<AgentWorkspaceHomePage src="https://workspace.example.test/synthetic" />);
 const preserved = screen.getByRole("textbox", { name: "保留的问题草稿" }) as HTMLTextAreaElement;
 expect(preserved.value).toBe(draft);
 expect(preserved.readOnly).toBe(true);
 expect(view.container.querySelector("iframe")?.getAttribute("src")).toBe("https://workspace.example.test/synthetic");
 expect(mocks.access).not.toHaveBeenCalled();
 expect(writeText).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button", { name: "复制草稿" }));
 await waitFor(() => expect(screen.getByRole("status").textContent).toBe("已复制，可粘贴到对话输入框。"));
 expect(writeText).toHaveBeenCalledWith(draft);
 authenticate("free");
 view.rerender(<AgentWorkspaceHomePage src="https://workspace.example.test/synthetic" />);
 expect((screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement).value).toBe(draft);
});

it("offers selection when clipboard access is unavailable and clears only on explicit request", () => {
 vi.stubGlobal("navigator", {});
 authenticate("free");
 const view = render(<AgentWorkspaceHomePage />);
 fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), { target: { value: "Synthetic retained question" } });
 authenticate("member");
 view.rerender(<AgentWorkspaceHomePage />);
 fireEvent.click(screen.getByRole("button", { name: "复制草稿" }));
 const preserved = screen.getByRole("textbox", { name: "保留的问题草稿" }) as HTMLTextAreaElement;
 expect(document.activeElement).toBe(preserved);
 expect(preserved.selectionStart).toBe(0);
 expect(preserved.selectionEnd).toBe(preserved.value.length);
 expect(screen.getByText("已选中草稿，请手动复制后粘贴到对话输入框。").getAttribute("role")).toBe("status");
 fireEvent.click(screen.getByRole("button", { name: "清除草稿" }));
 expect(screen.queryByRole("textbox", { name: "保留的问题草稿" })).toBeNull();
 authenticate("free");
 view.rerender(<AgentWorkspaceHomePage />);
 expect((screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement).value).toBe("");
});

it.each(["logout", "other-account", "new-session"] as const)("does not expose a retained question after %s", transition => {
 authenticate("free");
 const view = render(<AgentWorkspaceHomePage />);
 fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), { target: { value: "Private question from previous identity" } });
 authenticate("member");
 view.rerender(<AgentWorkspaceHomePage />);
 expect(screen.getByRole("textbox", { name: "保留的问题草稿" })).toBeTruthy();
 if (transition === "logout") mocks.auth = { status: "guest", session: undefined };
 else authenticate("member", transition === "other-account" ? "another-owner" : "owner", transition === "new-session" ? "new-session" : "same-session");
 view.rerender(<AgentWorkspaceHomePage />);
 expect(screen.queryByRole("textbox", { name: "保留的问题草稿" })).toBeNull();
 authenticate("free");
 view.rerender(<AgentWorkspaceHomePage />);
 expect((screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement).value).toBe("");
});
