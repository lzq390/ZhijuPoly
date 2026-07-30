// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  POLYTAO_DESCRIPTOR_NAMES,
  type PolytaoDescriptorMap,
  type PolytaoGenerationRequest,
  type PolytaoJobStatusResponse,
  type PolytaoStatusResponse,
  type StructureWorkspaceContext
} from "../types";
import { DEFAULT_POLYTAO_DESCRIPTORS } from "../hooks/usePolytaoGeneration";
import { PolytaoGenerationPage } from "./PolytaoGenerationPage";

const api = vi.hoisted(() => ({
  calculatePolytaoDescriptors: vi.fn(),
  createPolytaoJob: vi.fn(),
  fetchPolytaoJob: vi.fn(),
  fetchPolytaoStatus: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    calculatePolytaoDescriptors: api.calculatePolytaoDescriptors,
    createPolytaoJob: api.createPolytaoJob,
    fetchPolytaoJob: api.fetchPolytaoJob,
    fetchPolytaoStatus: api.fetchPolytaoStatus
  };
});

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="structure-preview">{smiles}</div>
  )
}));

const GROUPS = [
  {
    name: "Size / Composition",
    descriptors: ["MolWt", "HeavyAtomCount", "NumHeteroatoms"]
  },
  {
    name: "Donor / Acceptor",
    descriptors: ["NHOHCount", "NOCount", "NumHAcceptors", "NumHDonors"]
  },
  {
    name: "Ring System",
    descriptors: [
      "NumAliphaticCarbocycles",
      "NumAliphaticHeterocycles",
      "NumAliphaticRings",
      "NumAromaticCarbocycles",
      "NumAromaticHeterocycles",
      "NumAromaticRings",
      "RingCount"
    ]
  },
  {
    name: "Flexibility",
    descriptors: ["NumRotatableBonds"]
  }
] as const;

const PREFILL_DESCRIPTORS = Object.fromEntries(
  POLYTAO_DESCRIPTOR_NAMES.map((name, index) => [name, 100 + index])
) as PolytaoDescriptorMap;
const PREFILL_PROMPT = POLYTAO_DESCRIPTOR_NAMES.map((name) => PREFILL_DESCRIPTORS[name]).join(",");

function readyStatus(): PolytaoStatusResponse {
  return {
    enabled: true,
    available: true,
    worker_base_url_configured: true,
    worker_status: "ready",
    worker_mode: "local",
    db_configured: true,
    db_ready: true,
    db_error: null,
    runtime_ready: true,
    runtime_error: null,
    active_jobs: 0,
    model_id: "polytao",
    model_revision: "test",
    default_params: {},
    worker_version: "test",
    message: "PolyTAO runtime is ready"
  };
}

function completedJob(jobId: string): PolytaoJobStatusResponse {
  return {
    job_id: jobId,
    status: "completed",
    input_smiles: "canonical-prefill",
    canonical_smiles: "canonical-prefill",
    prompt: PREFILL_PROMPT,
    requested_count: 10,
    returned_count: 0,
    attempts: 1,
    progress_percent: 100,
    progress_stage: "completed",
    progress_message: "Completed",
    created_at: "2026-07-30T00:00:00Z",
    updated_at: "2026-07-30T00:00:01Z",
    started_at: "2026-07-30T00:00:00Z",
    finished_at: "2026-07-30T00:00:01Z",
    worker_id: "test-worker",
    worker_job_id: "test-worker-job",
    worker_version: "test",
    engine: "polytao-backend",
    error_message: null,
    result: null
  };
}

function makeStructure(
  overrides: Partial<StructureWorkspaceContext> = {}
): StructureWorkspaceContext {
  return {
    smiles: "CCO",
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue("C(C)O"),
    ...overrides
  };
}

function renderPage(structure = makeStructure()) {
  return render(
    <PolytaoGenerationPage
      structure={structure}
      onEditStructure={vi.fn()}
      onBackHome={vi.fn()}
    />
  );
}

function descriptorEditor(): HTMLElement {
  const panel = screen.getByRole("heading", { name: "Descriptor Editor" }).closest("section");
  if (!panel) {
    throw new Error("Descriptor Editor panel was not rendered.");
  }
  return panel;
}

function descriptorSelect(groupName: string): HTMLSelectElement {
  return screen.getByRole("combobox", { name: `${groupName} descriptor` }) as HTMLSelectElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchPolytaoStatus.mockResolvedValue(readyStatus());
  api.calculatePolytaoDescriptors.mockResolvedValue({
    input_smiles: "C(C)O",
    canonical_smiles: "canonical-prefill",
    descriptors: POLYTAO_DESCRIPTOR_NAMES.map((name) => ({
      name,
      value: PREFILL_DESCRIPTORS[name]
    })),
    prompt: PREFILL_PROMPT,
    query_time_ms: 3
  });
  api.createPolytaoJob.mockResolvedValue({
    job_id: "polytao-test-job",
    status: "submitted"
  });
  api.fetchPolytaoJob.mockImplementation(async (jobId: string) => completedJob(jobId));
});

afterEach(() => {
  cleanup();
});

describe("PolytaoGenerationPage descriptor editor", () => {
  it("renders four groups with one selector and one value input each, covering all 15 descriptors", () => {
    renderPage();

    const editor = descriptorEditor();
    const selects = within(editor).getAllByRole("combobox") as HTMLSelectElement[];
    const inputs = within(editor).getAllByRole("spinbutton") as HTMLInputElement[];

    expect(selects).toHaveLength(4);
    expect(inputs).toHaveLength(4);
    expect(selects.map((select) => select.value)).toEqual([
      "MolWt",
      "NHOHCount",
      "NumAliphaticCarbocycles",
      "NumRotatableBonds"
    ]);

    const optionValues = selects.flatMap((select) =>
      Array.from(select.options, (option) => option.value)
    );
    expect(optionValues).toHaveLength(15);
    expect(new Set(optionValues)).toEqual(new Set(POLYTAO_DESCRIPTOR_NAMES));
    expect(
      selects.flatMap((select) => Array.from(select.options)).every(
        (option) => option.textContent === `${option.value} — Empty`
      )
    ).toBe(true);

    for (const group of GROUPS) {
      expect(within(editor).getByText(group.name)).toBeTruthy();
      expect(within(editor).getByText(`Filled 0/${group.descriptors.length}`)).toBeTruthy();
    }
  });

  it("keeps hidden values and selector choices while loading, editing, and clearing descriptors", () => {
    renderPage();
    const editor = descriptorEditor();
    const sizeSelect = descriptorSelect("Size / Composition");

    fireEvent.click(screen.getByRole("button", { name: "Load Sample" }));

    expect(within(editor).getByText("Filled 3/3")).toBeTruthy();
    expect(within(editor).getByText("Filled 4/4")).toBeTruthy();
    expect(within(editor).getByText("Filled 7/7")).toBeTruthy();
    expect(within(editor).getByText("Filled 1/1")).toBeTruthy();
    expect(
      within(editor).getAllByRole("option").every((option) => option.textContent?.endsWith("— Filled"))
    ).toBe(true);
    expect(screen.getByLabelText("MolWt")).toHaveProperty("value", "264");

    fireEvent.change(sizeSelect, { target: { value: "HeavyAtomCount" } });
    const heavyAtomInput = screen.getByLabelText("HeavyAtomCount") as HTMLInputElement;
    expect(heavyAtomInput.value).toBe("19");
    fireEvent.change(heavyAtomInput, { target: { value: "23" } });

    fireEvent.change(sizeSelect, { target: { value: "NumHeteroatoms" } });
    expect(screen.getByLabelText("NumHeteroatoms")).toHaveProperty("value", "6");
    fireEvent.change(screen.getByLabelText("NumHeteroatoms"), { target: { value: "8" } });

    fireEvent.change(sizeSelect, { target: { value: "HeavyAtomCount" } });
    expect(screen.getByLabelText("HeavyAtomCount")).toHaveProperty("value", "23");
    expect(screen.getByText("264,23,0,4,1,0,1,0,0,0,4,0,8,5,1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(sizeSelect.value).toBe("HeavyAtomCount");
    expect(screen.getByLabelText("HeavyAtomCount")).toHaveProperty("value", "");
    for (const group of GROUPS) {
      expect(within(editor).getByText(`Filled 0/${group.descriptors.length}`)).toBeTruthy();
    }
    expect(
      within(editor).getAllByRole("option").every((option) => option.textContent?.endsWith("— Empty"))
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Submit PolyTAO Job" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Load Sample" }));
    expect(sizeSelect.value).toBe("HeavyAtomCount");
    expect(screen.getByLabelText("HeavyAtomCount")).toHaveProperty("value", "19");
  });

  it("updates completion state when one empty descriptor is filled manually", () => {
    renderPage();
    const editor = descriptorEditor();

    expect(within(editor).getByText("Filled 0/3")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("MolWt"), { target: { value: "101.5" } });

    expect(within(editor).getByText("Filled 1/3")).toBeTruthy();
    expect(within(descriptorSelect("Size / Composition")).getByRole("option", { name: "MolWt — Filled" })).toBeTruthy();
    expect(
      within(descriptorSelect("Size / Composition")).getByRole("option", {
        name: "HeavyAtomCount — Empty"
      })
    ).toBeTruthy();
  });

  it("prefills all descriptors without resetting selection and submits the complete ordered payload", async () => {
    const structure = makeStructure();
    renderPage(structure);
    const sizeSelect = descriptorSelect("Size / Composition");

    fireEvent.change(sizeSelect, { target: { value: "NumHeteroatoms" } });
    fireEvent.click(screen.getByRole("button", { name: "Prefill Descriptors" }));

    await waitFor(() => {
      expect(api.calculatePolytaoDescriptors).toHaveBeenCalledWith({ smiles: "C(C)O" });
      expect(screen.getByText(PREFILL_PROMPT)).toBeTruthy();
    });

    expect(sizeSelect.value).toBe("NumHeteroatoms");
    fireEvent.change(sizeSelect, { target: { value: "MolWt" } });
    expect(screen.getByLabelText("MolWt")).toHaveProperty("value", "100");
    fireEvent.change(sizeSelect, { target: { value: "NumHeteroatoms" } });
    expect(sizeSelect.value).toBe("NumHeteroatoms");
    expect(screen.getByLabelText("NumHeteroatoms")).toHaveProperty("value", "112");
    expect(screen.getByText("Filled 3/3")).toBeTruthy();
    expect(screen.getByText("Filled 4/4")).toBeTruthy();
    expect(screen.getByText("Filled 7/7")).toBeTruthy();
    expect(screen.getByText("Filled 1/1")).toBeTruthy();
    expect(screen.queryByText(/Source:/)).toBeNull();
    expect(screen.queryByText(/canonical-prefill/)).toBeNull();

    const submit = screen.getByRole("button", { name: "Submit PolyTAO Job" }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);

    await waitFor(() => expect(api.createPolytaoJob).toHaveBeenCalledOnce());
    const submitted = api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest;
    expect(Object.keys(submitted.descriptors)).toEqual([...POLYTAO_DESCRIPTOR_NAMES]);
    expect(submitted.descriptors).toEqual(PREFILL_DESCRIPTORS);
    expect(submitted.input_smiles).toBe("canonical-prefill");
    await waitFor(() => expect(api.fetchPolytaoJob).toHaveBeenCalledWith("polytao-test-job"));
  });

  it("clears the prefilled SMILES association only after a descriptor value is edited", async () => {
    renderPage();
    const sizeSelect = descriptorSelect("Size / Composition");

    fireEvent.click(screen.getByRole("button", { name: "Prefill Descriptors" }));
    await waitFor(() => expect(screen.getByText(PREFILL_PROMPT)).toBeTruthy());

    fireEvent.change(sizeSelect, { target: { value: "HeavyAtomCount" } });
    fireEvent.change(screen.getByLabelText("HeavyAtomCount"), { target: { value: "119" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit PolyTAO Job" }));

    await waitFor(() => expect(api.createPolytaoJob).toHaveBeenCalledOnce());
    const submitted = api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest;
    expect(submitted.descriptors.HeavyAtomCount).toBe(119);
    expect(submitted.input_smiles).toBeNull();
  });

  it("clears the prefilled SMILES association when the sample vector is loaded", async () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Prefill Descriptors" }));
    await waitFor(() => expect(screen.getByText(PREFILL_PROMPT)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Load Sample" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit PolyTAO Job" }));

    await waitFor(() => expect(api.createPolytaoJob).toHaveBeenCalledOnce());
    const submitted = api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest;
    expect(submitted.descriptors).toEqual(DEFAULT_POLYTAO_DESCRIPTORS);
    expect(submitted.input_smiles).toBeNull();
  });

  it("keeps Current SMILES and the structure preview and still surfaces prefill errors", async () => {
    api.calculatePolytaoDescriptors.mockRejectedValueOnce(new Error("Descriptor prefill failed."));
    renderPage();

    const sourcePanel = screen.getByRole("heading", { name: "Structure Source" }).closest("section");
    if (!sourcePanel) {
      throw new Error("Structure Source panel was not rendered.");
    }

    expect(within(sourcePanel).getByText("Current SMILES")).toBeTruthy();
    expect(within(sourcePanel).getAllByText("CCO")).toHaveLength(2);
    expect(within(sourcePanel).getByTestId("structure-preview")).toBeTruthy();
    expect(within(sourcePanel).queryByText(/Source:/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Prefill Descriptors" }));

    expect(await screen.findByText("Descriptor prefill failed.")).toBeTruthy();
    expect(within(sourcePanel).getByText("Current SMILES")).toBeTruthy();
    expect(within(sourcePanel).getByTestId("structure-preview")).toBeTruthy();
    expect(within(sourcePanel).queryByText(/Source:/)).toBeNull();
  });
});
