/**
 * Approximate structure descriptors for `npm run dev:mock` only.
 *
 * The real values come from RDKit on the backend. This small SMILES reader covers the organic
 * subset well enough for the interface preview: formula, weight and atom counts match RDKit for
 * ordinary repeat units; ring aromaticity, rotatable bonds and TPSA follow simplified rules.
 */

type Atom = {
  symbol: string;
  aromatic: boolean;
  dummy: boolean;
  bracketHydrogens: number | null;
  charge: number;
};
type Bond = { a: number; b: number; order: number };

const WEIGHTS: Record<string, number> = {
  H: 1.008, B: 10.81, C: 12.011, N: 14.007, O: 15.999, F: 18.998, Si: 28.085, P: 30.974,
  S: 32.06, Cl: 35.45, Ge: 72.63, As: 74.922, Se: 78.971, Br: 79.904, Sn: 118.71, I: 126.904
};
const VALENCES: Record<string, number[]> = {
  B: [3], C: [4], N: [3, 5], O: [2], P: [3, 5], S: [2, 4, 6], F: [1], Cl: [1], Br: [1], I: [1]
};
const ORGANIC = /^(Cl|Br|B|C|N|O|P|S|F|I|b|c|n|o|p|s|\*)/;
const BOND_ORDERS: Record<string, number> = { "-": 1, "=": 2, "#": 3, $: 4, ":": 1.5, "/": 1, "\\": 1 };

function parseBracket(text: string): Atom {
  const match = /^(\d*)(\*|[A-Z][a-z]?|[a-z][a-z]?)(@*)(H\d*)?([+-]+\d*)?(:\d+)?$/.exec(text);
  if (!match) throw new Error(`invalid atom [${text}]`);
  const raw = match[2];
  const aromatic = raw !== "*" && raw[0] === raw[0].toLowerCase();
  const symbol = aromatic ? raw[0].toUpperCase() + raw.slice(1) : raw;
  const hydrogens = match[4] ? Number(match[4].slice(1) || 1) : 0;
  let charge = 0;
  if (match[5]) {
    const sign = match[5][0] === "+" ? 1 : -1;
    const digits = match[5].replace(/[+-]/g, "");
    charge = sign * (digits ? Number(digits) : match[5].length);
  }
  return { symbol, aromatic, dummy: raw === "*", bracketHydrogens: hydrogens, charge };
}

export function parseSmiles(input: string): { atoms: Atom[]; bonds: Bond[] } {
  // CXSMILES extensions such as |$star_e;…$| carry no atoms or bonds.
  const text = input.trim().split(/\s+/)[0] ?? "";
  if (!text) throw new Error("empty smiles");
  const atoms: Atom[] = [];
  const bonds: Bond[] = [];
  const branches: number[] = [];
  const rings = new Map<string, { atom: number; order: number | null }>();
  let previous: number | null = null;
  let pendingOrder: number | null = null;
  const connect = (a: number, b: number, order: number | null) => {
    const aromatic = atoms[a].aromatic && atoms[b].aromatic;
    bonds.push({ a, b, order: order ?? (aromatic ? 1.5 : 1) });
  };
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    let atom: Atom | null = null;
    if (char === "[") {
      const end = text.indexOf("]", index);
      if (end < 0) throw new Error("unclosed bracket");
      atom = parseBracket(text.slice(index + 1, end));
      index = end + 1;
    } else if (ORGANIC.test(text.slice(index))) {
      const raw = ORGANIC.exec(text.slice(index))![0];
      const aromatic = raw !== "*" && raw === raw.toLowerCase();
      atom = { symbol: aromatic ? raw.toUpperCase() : raw, aromatic, dummy: raw === "*", bracketHydrogens: null, charge: 0 };
      index += raw.length;
    } else if (char in BOND_ORDERS) {
      pendingOrder = BOND_ORDERS[char];
      index += 1;
      continue;
    } else if (char === "(") {
      if (previous === null) throw new Error("branch before atom");
      branches.push(previous);
      index += 1;
      continue;
    } else if (char === ")") {
      const top = branches.pop();
      if (top === undefined) throw new Error("unbalanced branch");
      previous = top;
      index += 1;
      continue;
    } else if (char === ".") {
      previous = null;
      index += 1;
      continue;
    } else if (/[0-9%]/.test(char)) {
      const label = char === "%" ? text.slice(index, index + 3) : char;
      index += label.length;
      if (previous === null) throw new Error("ring closure before atom");
      const open = rings.get(label);
      if (open) {
        connect(open.atom, previous, pendingOrder ?? open.order);
        rings.delete(label);
      } else {
        rings.set(label, { atom: previous, order: pendingOrder });
      }
      pendingOrder = null;
      continue;
    } else {
      throw new Error(`unexpected "${char}"`);
    }
    atoms.push(atom);
    const current = atoms.length - 1;
    if (previous !== null) connect(previous, current, pendingOrder);
    pendingOrder = null;
    previous = current;
  }
  if (rings.size || branches.length) throw new Error("unclosed ring or branch");
  return { atoms, bonds };
}

function round(value: number, digits: number) {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

export function approximateDescriptors(smiles: string) {
  const { atoms, bonds } = parseSmiles(smiles);
  const neighbours = atoms.map(() => [] as Array<{ atom: number; bond: Bond }>);
  for (const bond of bonds) {
    neighbours[bond.a].push({ atom: bond.b, bond });
    neighbours[bond.b].push({ atom: bond.a, bond });
  }
  const hydrogens = atoms.map((atom, index) => {
    if (atom.dummy) return 0;
    if (atom.bracketHydrogens !== null) return atom.bracketHydrogens;
    const used = neighbours[index].reduce((sum, item) => sum + item.bond.order, 0);
    const choices = VALENCES[atom.symbol] ?? [0];
    // Aromatic atoms keep their lowest valence (thiophene s, furan o and pyridine n carry no H).
    const valence = atom.aromatic ? choices[0] : choices.find((value) => value >= used) ?? used;
    return Math.max(0, Math.floor(valence - used + 1e-9));
  });

  // Ring membership: a bond is in a ring when its ends stay connected without it.
  const connected = (from: number, to: number, skip: Bond) => {
    const seen = new Set([from]);
    const queue = [from];
    while (queue.length) {
      const next = queue.shift()!;
      if (next === to) return true;
      for (const { atom, bond } of neighbours[next]) {
        if (bond !== skip && !seen.has(atom)) {
          seen.add(atom);
          queue.push(atom);
        }
      }
    }
    return false;
  };
  const ringBond = new Set(bonds.filter((bond) => connected(bond.a, bond.b, bond)));
  const shortestRing = (bond: Bond) => {
    const previous = new Map<number, number>([[bond.a, -1]]);
    const queue = [bond.a];
    while (queue.length) {
      const next = queue.shift()!;
      if (next === bond.b) break;
      for (const { atom, bond: edge } of neighbours[next]) {
        if (edge !== bond && !previous.has(atom)) {
          previous.set(atom, next);
          queue.push(atom);
        }
      }
    }
    const ring: number[] = [];
    for (let atom = bond.b; atom !== -1; atom = previous.get(atom) ?? -1) ring.push(atom);
    return ring;
  };
  // One ring per independent cycle (bonds − atoms + fragments), found from the ring-closure bonds.
  const fragments = (() => {
    const seen = new Set<number>();
    let count = 0;
    atoms.forEach((_, start) => {
      if (seen.has(start)) return;
      count += 1;
      const queue = [start];
      seen.add(start);
      while (queue.length) for (const { atom } of neighbours[queue.shift()!]) if (!seen.has(atom)) { seen.add(atom); queue.push(atom); }
    });
    return count;
  })();
  const ringCount = Math.max(0, bonds.length - atoms.length + fragments);
  const rings: number[][] = [];
  const seenRings = new Set<string>();
  for (const bond of ringBond) {
    const ring = shortestRing(bond);
    const key = [...ring].sort((x, y) => x - y).join(",");
    if (!seenRings.has(key)) {
      seenRings.add(key);
      rings.push(ring);
    }
  }
  const aromaticRings = rings.slice(0, ringCount).filter((ring) => ring.every((atom) => atoms[atom].aromatic)).length;

  const heavy = (index: number) => !atoms[index].dummy && atoms[index].symbol !== "H";
  const degree = (index: number) => neighbours[index].filter(({ atom }) => atoms[atom].symbol !== "H").length;
  const carbonylCarbon = (index: number) =>
    atoms[index].symbol === "C" && neighbours[index].some(({ atom, bond }) => bond.order === 2 && atoms[atom].symbol === "O");
  const tripleBonded = (index: number) => neighbours[index].some(({ bond }) => bond.order === 3);
  const rotatable = bonds.filter((bond) => {
    if (bond.order !== 1 || ringBond.has(bond)) return false;
    if (degree(bond.a) < 2 || degree(bond.b) < 2 || tripleBonded(bond.a) || tripleBonded(bond.b)) return false;
    // Esters and amides are planar; RDKit's strict count leaves them out.
    const [x, y] = [bond.a, bond.b];
    const hetero = (index: number) => atoms[index].symbol === "O" || atoms[index].symbol === "N";
    return !((carbonylCarbon(x) && hetero(y)) || (carbonylCarbon(y) && hetero(x)));
  }).length;

  let tpsa = 0;
  let donors = 0;
  let acceptors = 0;
  atoms.forEach((atom, index) => {
    const h = hydrogens[index];
    const orders = neighbours[index].map(({ bond }) => bond.order);
    if (atom.symbol === "N") {
      if (h > 0) donors += 1;
      if (atom.aromatic) tpsa += h > 0 ? 15.79 : orders.length >= 3 ? 4.93 : 12.89;
      else if (orders.includes(3)) tpsa += 23.79;
      else if (orders.includes(2)) tpsa += 12.36;
      else tpsa += [3.24, 12.03, 26.02][Math.min(h, 2)];
      const amide = neighbours[index].some(({ atom: other }) => carbonylCarbon(other));
      if (!amide && h === 0) acceptors += 1;
    } else if (atom.symbol === "O") {
      if (h > 0) donors += 1;
      acceptors += 1;
      tpsa += atom.aromatic ? 13.14 : orders.includes(2) ? 17.07 : h > 0 ? 20.23 : 9.23;
    } else if (atom.symbol === "S" && (atom.aromatic || orders.every((order) => order === 1))) {
      acceptors += 1;
    } else if (atom.symbol === "F") {
      acceptors += 1;
    }
  });

  const counts = new Map<string, number>();
  atoms.forEach((atom, index) => {
    if (atom.dummy) return;
    counts.set(atom.symbol, (counts.get(atom.symbol) ?? 0) + 1);
    if (hydrogens[index]) counts.set("H", (counts.get("H") ?? 0) + hydrogens[index]);
  });
  const order = [...counts.keys()].sort((x, y) => {
    const rank = (symbol: string) => (counts.has("C") ? (symbol === "C" ? 0 : symbol === "H" ? 1 : 2) : 2);
    return rank(x) - rank(y) || x.localeCompare(y);
  });
  const formula = order.map((symbol) => `${symbol}${counts.get(symbol)! > 1 ? counts.get(symbol) : ""}`).join("");
  const weight = [...counts].reduce((sum, [symbol, count]) => sum + (WEIGHTS[symbol] ?? 0) * count, 0);
  const carbons = atoms.filter((atom) => atom.symbol === "C");
  const sp3 = atoms.filter((atom, index) =>
    atom.symbol === "C" && !atom.aromatic && neighbours[index].every(({ bond }) => bond.order === 1)
  ).length;

  return {
    canonical_smiles: smiles.trim().split(/\s+/)[0],
    formula,
    molecular_weight: round(weight, 3),
    heavy_atom_count: atoms.filter((_, index) => heavy(index)).length,
    attachment_point_count: atoms.filter((atom) => atom.dummy).length,
    ring_count: ringCount,
    aromatic_ring_count: aromaticRings,
    rotatable_bond_count: rotatable,
    hbond_donor_count: donors,
    hbond_acceptor_count: acceptors,
    heteroatom_count: atoms.filter((atom, index) => heavy(index) && atom.symbol !== "C").length,
    tpsa: round(tpsa, 2),
    fraction_csp3: carbons.length ? round(sp3 / carbons.length, 3) : 0
  };
}
