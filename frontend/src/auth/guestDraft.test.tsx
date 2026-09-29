// @vitest-environment jsdom
import { StrictMode, Suspense } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useStructureWorkspace } from "../hooks/useStructureWorkspace";
import { acceptGuestDraft, commitGuestDraft, readGuestDraft } from "./guestDraft";
import { retireSession } from "./session";

vi.mock("../services/api", () => ({ standardizeSmiles: vi.fn() }));
afterEach(() => { cleanup(); retireSession(); });

it("retains the confirmed guest structure when the first workspace render is retried", async () => {
  acceptGuestDraft("CCO");
  let ready = false;
  let finish!: () => void;
  const loading = new Promise<void>(resolve => { finish = resolve; });
  function Workspace() {
    const structure = useStructureWorkspace();
    if (!ready) throw loading;
    return <output data-testid="structure">{structure.smiles}</output>;
  }
  render(<StrictMode><Suspense fallback={<p>Loading</p>}><Workspace /></Suspense></StrictMode>);
  await act(async () => { ready = true; finish(); await loading; });
  expect(screen.getByTestId("structure").textContent).toBe("CCO");
});

it("imports once after a workspace commits, and clears pending imports on identity change", () => {
  function Workspace() {
    const structure = useStructureWorkspace();
    return <output data-testid="structure">{structure.smiles}</output>;
  }
  acceptGuestDraft("CCO");
  const first = render(<StrictMode><Workspace /></StrictMode>);
  expect(screen.getByTestId("structure").textContent).toBe("CCO");
  first.unmount();
  const second = render(<Workspace />);
  expect(screen.getByTestId("structure").textContent).toBe("");
  second.unmount();
  acceptGuestDraft("CCN");
  retireSession();
  render(<Workspace />);
  expect(screen.getByTestId("structure").textContent).toBe("");
});

it("does not let an older workspace consume a newer confirmed import", () => {
  acceptGuestDraft("CCO");
  const previous = readGuestDraft();
  retireSession();
  acceptGuestDraft("CCN");
  commitGuestDraft(previous);
  expect(readGuestDraft()?.smiles).toBe("CCN");
});
