export type HomopolymerExample = {
  abbr: string;
  name: string;
  smiles: string;
  image: string;
};

/** Starting points for an empty canvas; images are static RDKit drawings under public/samples. */
export const HOMOPOLYMER_EXAMPLES: readonly HomopolymerExample[] = [
  { abbr: "PE", name: "聚乙烯", smiles: "*CC*", image: "/samples/homopolymer-examples/pe.svg" },
  { abbr: "PS", name: "聚苯乙烯", smiles: "*CC(*)c1ccccc1", image: "/samples/homopolymer-examples/ps.svg" },
  { abbr: "PMMA", name: "聚甲基丙烯酸甲酯", smiles: "*CC(*)(C)C(=O)OC", image: "/samples/homopolymer-examples/pmma.svg" },
  {
    abbr: "PI",
    name: "聚酰亚胺 PMDA-ODA",
    smiles: "*c1ccc(Oc2ccc(N3C(=O)c4cc5C(=O)N(*)C(=O)c5cc4C3=O)cc2)cc1",
    image: "/samples/homopolymer-examples/pi-pmda-oda.svg"
  }
];
