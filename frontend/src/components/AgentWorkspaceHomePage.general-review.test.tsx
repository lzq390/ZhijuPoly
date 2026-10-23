/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AgentWorkspaceHomePage } from "./AgentWorkspaceHomePage";
const mocks = vi.hoisted(() => ({ access: vi.fn(() => false) }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => ({ status: "guest" }) }));
vi.mock("../auth/guestAccess", () => ({ requestServiceAccess: mocks.access }));
beforeEach(() => mocks.access.mockClear()); afterEach(() => cleanup());
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
