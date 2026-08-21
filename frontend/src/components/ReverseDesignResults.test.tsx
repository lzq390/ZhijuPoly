// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ReverseDesignTgCandidate,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse
} from "../types";
import { ReverseDesignResults } from "./ReverseDesignResults";

function candidate(index: number): ReverseDesignTgCandidate {
  return {
    rank: index,
    pi_id: index,
    polymer_smiles: `*CC${index}*`,
    canonical_polym: null,
    monomer_a_smiles: `A${index}`,
    monomer_b_smiles: `B${index}`,
    monomer_a_iupac: `IUPAC A ${index}`,
    monomer_b_iupac: `IUPAC B ${index}`,
    monomer_a_structure_svg: null,
    monomer_b_structure_svg: null,
    tg_value: 440 + index,
    tg_unit: "°C",
    tg_difference: index - 5,
    similarity_score: 0.7 + index / 100,
    structure_svg: null,
    knowledge_available: true
  };
}

const request: ReverseDesignTgRequest = {
  target_tg: 450,
  smiles: "*CC*",
  similarity_threshold: 0.7,
  candidate_size: 200
};

const data: ReverseDesignTgResponse = {
  target_tg: 450,
  query_time_ms: 12,
  candidate_pool_size: 2000,
  sampled_candidate_count: 10,
  total: 6,
  data_source: "pi_reverse_design",
  results: Array.from({ length: 6 }, (_, index) => candidate(index + 1))
};

afterEach(() => {
  cleanup();
});

describe("ReverseDesignResults drawer content", () => {
  it("shows exactly five candidates per page and supports paging", () => {
    render(
      <ReverseDesignResults
        data={data}
        error={null}
        submittedRequest={request}
        onOpenKnowledge={vi.fn()}
      />
    );

    expect(screen.getAllByText(/^PI \d+$/)).toHaveLength(5);
    expect(screen.getByText("PI 1")).toBeTruthy();
    expect(screen.queryByText("PI 6")).toBeNull();

    fireEvent.click(screen.getAllByRole("button", { name: "下一页" })[0]);
    expect(screen.getAllByText(/^PI \d+$/)).toHaveLength(1);
    expect(screen.getByText("PI 6")).toBeTruthy();
  });

  it("expands IUPAC content and sends the selected knowledge terms", () => {
    const onOpenKnowledge = vi.fn();
    render(
      <ReverseDesignResults
        data={{ ...data, total: 1, results: [candidate(1)] }}
        error={null}
        submittedRequest={request}
        onOpenKnowledge={onOpenKnowledge}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "IUPAC" }));
    fireEvent.click(screen.getByRole("button", { name: /知识检索/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "A + B" }));

    expect(screen.getByText("IUPAC A 1")).toBeTruthy();
    expect(onOpenKnowledge).toHaveBeenCalledWith({
      query: "IUPAC A 1；IUPAC B 1",
      groups: [{ terms: ["IUPAC A 1"] }, { terms: ["IUPAC B 1"] }]
    });
  });
});
