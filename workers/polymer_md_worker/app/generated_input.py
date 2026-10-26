"""Inspect the actual frozen LAMMPS inputs without importing a scientific engine.

This is deliberately a closed protocol reader, not a LAMMPS interpreter. Only
literal variables used by the pinned templates are substituted; no eval, shell,
or arbitrary expressions are accepted as scientific configuration evidence.
"""
from __future__ import annotations

import hashlib
import ast
import math
from pathlib import Path
import re
import shlex


BONDED_STYLES = {
    name: {"bond": "harmonic", "angle": "harmonic",
           "dihedral": "harmonic" if name.startswith("Dreiding") else "fourier",
           "improper": "umbrella" if name.startswith("Dreiding") else "cvff"}
    for name in ("GAFF", "GAFF2", "GAFF2_mod", "Dreiding", "Dreiding_UT")
}


def _finite(value):
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise RuntimeError("generated scientific input contains a non-finite number")
    return float(value)


def forcefield_snapshot(mol, name: str) -> dict:
    """Independent coefficients from the assigned molecular state, in type order."""
    if name not in BONDED_STYLES:
        raise RuntimeError("unsupported forcefield evidence")
    styles = BONDED_STYLES[name]
    if mol.GetProp("pair_style") != "lj" or any(mol.GetProp(key + "_style") != value for key, value in styles.items()):
        raise RuntimeError("assigned molecular styles differ from the selected forcefield")
    coefficients = {key: [] for key in ("Masses", "Pair Coeffs", "Bond Coeffs", "Angle Coeffs", "Dihedral Coeffs", "Improper Coeffs")}
    seen = {}
    atoms, topology = [], {name: [] for name in ("Bonds", "Angles", "Dihedrals", "Impropers")}

    def coefficient(section, key, values):
        values = [_finite(value) for value in values]
        identity = (section, key)
        if identity in seen:
            index, previous = seen[identity]
            if values != previous:
                raise RuntimeError("one native forcefield type has inconsistent coefficients")
            return index
        index = len(coefficients[section]) + 1
        coefficients[section].append(values)
        seen[identity] = index, values
        return index

    for atom in mol.GetAtoms():
        isotope = 0 if atom.GetSymbol() == "H" and atom.GetIsotope() >= 3 else atom.GetIsotope()
        key = (atom.GetProp("ff_type"), isotope)
        identifier = coefficient("Pair Coeffs", key, [atom.GetDoubleProp("ff_epsilon"), atom.GetDoubleProp("ff_sigma")])
        coefficient("Masses", key, [1.008 if atom.GetSymbol() == "H" and atom.GetIsotope() >= 3 else atom.GetMass()])
        atoms.append([identifier, _finite(atom.GetDoubleProp("AtomicCharge"))])
    for bond in mol.GetBonds():
        identifier = coefficient("Bond Coeffs", bond.GetProp("ff_type"), [bond.GetDoubleProp("ff_k"), bond.GetDoubleProp("ff_r0")])
        topology["Bonds"].append([identifier, bond.GetBeginAtomIdx() + 1, bond.GetEndAtomIdx() + 1])
    for angle in getattr(mol, "angles", {}).values():
        identifier = coefficient("Angle Coeffs", angle.ff.type, [angle.ff.k, angle.ff.theta0])
        topology["Angles"].append([identifier, angle.a + 1, angle.b + 1, angle.c + 1])
    for dihedral in getattr(mol, "dihedrals", {}).values():
        ff = dihedral.ff
        values = ([ff.m, *[value for row in zip(ff.k, ff.n, ff.d0) for value in row]]
                  if styles["dihedral"] == "fourier" else [ff.k, ff.d0, ff.n])
        identifier = coefficient("Dihedral Coeffs", ff.type, values)
        topology["Dihedrals"].append([identifier, dihedral.a + 1, dihedral.b + 1, dihedral.c + 1, dihedral.d + 1])
    for improper in getattr(mol, "impropers", {}).values():
        ff = improper.ff
        identifier = coefficient("Improper Coeffs", ff.type, [ff.k, ff.x0] if styles["improper"] == "umbrella" else [ff.k, ff.d0, ff.n])
        topology["Impropers"].append([identifier, improper.a + 1, improper.b + 1, improper.c + 1, improper.d + 1])
    counts = {"atoms": mol.GetNumAtoms(), "bonds": mol.GetNumBonds(),
              **{kind: len(getattr(mol, kind, {})) for kind in ("angles", "dihedrals", "impropers")}}
    neighbors = [[] for _ in atoms]
    for _, left, right in topology["Bonds"]:
        neighbors[left - 1].append(right - 1); neighbors[right - 1].append(left - 1)
    molecule_ids, identifier = [0] * len(atoms), 0
    for start in range(len(atoms)):
        if molecule_ids[start]:
            continue
        identifier += 1; pending = [start]
        while pending:
            index = pending.pop()
            if not molecule_ids[index]:
                molecule_ids[index] = identifier; pending.extend(neighbors[index])
    return {"forcefield": name, "styles": {"pair": "lj/charmm/coul/long", **styles},
            "counts": counts, "coefficients": coefficients, "atom_types_and_charges": atoms,
            "molecule_ids": molecule_ids, "topology": topology}


def _commands(text: str):
    commands, variables, pending, fixes = [], {}, "", set()
    symbols = {"xlo", "xhi", "ylo", "yhi", "zlo", "zhi", "lx", "ly", "lz", "f_mp"}
    def resolve(line):
        if "$(" in line or re.search(r"\$(?!\{)", line):
            raise RuntimeError("unsupported dynamic LAMMPS substitution")
        def replace(match):
            if match[1] not in variables:
                raise RuntimeError("undefined generated LAMMPS variable: " + match[1])
            return variables[match[1]][1]
        return re.sub(r"\$\{(\w+)\}", replace, line)
    def expression(value):
        try:
            tree = ast.parse(value, mode="eval")
        except SyntaxError as error:
            raise RuntimeError("unsupported generated equal expression") from error
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                if isinstance(node.value, bool) or not isinstance(node.value, (int, float)) or not math.isfinite(node.value):
                    raise RuntimeError("unsupported equal expression constant")
            elif isinstance(node, ast.Name):
                native_msd = re.fullmatch(r"f_msd\d+", node.id) and node.id[2:] in fixes
                if node.id not in symbols and not native_msd:
                    raise RuntimeError("unsupported dynamic equal expression symbol")
            elif not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub, ast.Load)):
                raise RuntimeError("unsupported generated equal expression")
    def process(line):
        words = shlex.split(line)
        if not words:
            return
        if words[0] == "if":
            # The pinned templates use literal axis branches and fixed output
            # Booleans. Interpret only this closed subset, never arbitrary code.
            position, chosen = 1, False
            while position < len(words):
                if words[position] == "else":
                    condition, position = True, position + 1
                else:
                    if words[position] == "elif": position += 1
                    value = resolve(words[position]); position += 1
                    if value in {"0", "1"}:
                        condition = value == "1"
                    else:
                        match = re.fullmatch(r"([A-Za-z0-9_.+-]+) == ([A-Za-z0-9_.+-]+)", value)
                        if not match: raise RuntimeError("unsupported generated conditional expression")
                        condition = match[1] == match[2]
                    if position < len(words) and words[position] == "then": position += 1
                end = position
                while end < len(words) and words[end] not in {"elif", "else"}: end += 1
                for command in words[position:end]:
                    resolve(command)  # Undefined substitutions are invalid even in an inactive branch.
                if condition and not chosen:
                    for command in words[position:end]: process(command)
                    chosen = True
                position = end
            return
        if words[0] == "variable":
            if len(words) == 3 and words[2] == "delete":
                variables.pop(words[1], None); commands.append(line); return
            if len(words) != 4 or words[2] not in {"equal", "string"}:
                raise RuntimeError("unsupported generated variable definition")
            name, style, value = words[1], words[2], resolve(words[3])
            if style == "equal": expression(value)
            elif not re.fullmatch(r"[A-Za-z0-9_./+(),-]+", value):
                raise RuntimeError("unsupported generated string variable")
            if name in variables and variables[name] != (style, value) and (style != "equal" or variables[name][0] != "equal"):
                raise RuntimeError("generated variable requires explicit delete before changing its style or string")
            variables[name] = style, value
            commands.append("variable " + name + " " + style + " " + value)
            return
        if words[0] == "fix" and len(words) > 1: fixes.add(words[1])
        if words[0] == "unfix" and len(words) > 1: fixes.discard(words[1])
        commands.append(resolve(" ".join(words)))
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        continuation = line.endswith("&")
        pending += " " + line.removesuffix("&").strip()
        if not continuation:
            process(pending.strip()); pending = ""
    if pending: raise RuntimeError("unterminated generated input continuation")
    return commands


def _one(commands, prefix):
    matches = [line for line in commands if line.startswith(prefix + " ")]
    if len(matches) != 1:
        raise RuntimeError("missing or overridden generated command: " + prefix)
    return matches[0].split()[1:]


def _data_evidence(path: Path, snapshot: dict) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("unsafe or missing LAMMPS data file")
    counts, sections, section = {}, {}, None
    section_names = {*snapshot["coefficients"], "Atoms", "Bonds", "Angles", "Dihedrals", "Impropers", "Velocities"}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        count = re.fullmatch(r"(\d+) (atoms|bonds|angles|dihedrals|impropers|atom types|bond types|angle types|dihedral types|improper types)", " ".join(line.split()))
        if count:
            if count[2] in counts:
                raise RuntimeError("duplicate data header count")
            counts[count[2]] = int(count[1]); continue
        if line in section_names:
            if line in sections:
                raise RuntimeError("duplicate LAMMPS data section")
            section = line; sections[section] = []; continue
        if section and re.match(r"[A-Za-z]", line):
            section = None
        if section:
            try:
                fields = [_finite(value) for value in line.split()]
            except (ValueError, OverflowError) as error:
                raise RuntimeError("invalid numeric LAMMPS data row") from error
            sections[section].append(fields)
    for key, count in snapshot["counts"].items():
        if counts.get(key, 0) != count:
            raise RuntimeError("LAMMPS data topology count differs from assigned molecule: " + key)
    type_names = {"Masses": "atom", "Pair Coeffs": "atom", "Bond Coeffs": "bond", "Angle Coeffs": "angle", "Dihedral Coeffs": "dihedral", "Improper Coeffs": "improper"}
    result = {}
    for name, expected in snapshot["coefficients"].items():
        rows = sorted(sections.get(name, []), key=lambda row: row[0])
        if counts.get(type_names[name] + " types", 0) != len(expected) or len(rows) != len(expected):
            raise RuntimeError("incomplete forcefield coefficient table: " + name)
        for index, (actual, wanted) in enumerate(zip(rows, expected), 1):
            if (len(actual) != len(wanted) + 1 or actual[0] != index
                    or any(not math.isclose(a, b, rel_tol=1e-10, abs_tol=5.1e-7) for a, b in zip(actual[1:], wanted))):
                raise RuntimeError("generated coefficient differs from assigned forcefield: " + name)
        encoded = "\n".join(" ".join(map(str, row)) for row in rows).encode()
        result[name] = {"type_count": len(expected), "sha256": hashlib.sha256(encoded).hexdigest()}
    atom_rows = sorted(sections.get("Atoms", []), key=lambda row: row[0])
    if len(atom_rows) != snapshot["counts"]["atoms"]:
        raise RuntimeError("incomplete atom table")
    for index, (actual, wanted) in enumerate(zip(atom_rows, snapshot["atom_types_and_charges"]), 1):
        if (len(actual) < 7 or actual[0] != index or actual[1] != snapshot["molecule_ids"][index - 1] or actual[2] != wanted[0]
                or not math.isclose(actual[3], wanted[1], rel_tol=1e-10, abs_tol=5.1e-7)):
            raise RuntimeError("atom type or charge differs from assigned molecular state")
    for name, expected in snapshot["topology"].items():
        rows = sorted(sections.get(name, []), key=lambda row: row[0])
        if len(rows) != len(expected) or any(row[0] != i for i, row in enumerate(rows, 1)):
            raise RuntimeError("incomplete molecular topology table: " + name)
        def canonical(values):
            values = tuple(values)
            reverse = (values[0], *values[:0:-1])
            return min(values, reverse) if name != "Impropers" else values
        if sorted(canonical(row[1:]) for row in rows) != sorted(canonical(row) for row in expected):
            raise RuntimeError("generated topology differs from assigned molecular state: " + name)
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "counts": snapshot["counts"], "coefficient_tables": result}


def segment(steps, temp, stop=None, *, pressure=None, dt=1.):
    return {"steps": int(steps), "time_step_fs": dt, "ensemble": "npt" if pressure is not None else "nvt",
            "temperature_start_k": float(temp), "temperature_stop_k": float(temp if stop is None else stop),
            **({"pressure_start_atm": float(pressure), "pressure_stop_atm": float(pressure)} if pressure is not None else {})}


def equilibrium_schedule(request, stage):
    if stage == "eq1":
        return [segment(20000, 300, dt=.1), segment(1000000, 300, 700), segment(1000000, 700)]
    if stage == "eq2":
        steps = [[50000, 50000, 50000], [50000, 100000, 50000], [50000, 100000, 50000],
                 [50000, 100000, 5000], [5000, 10000, 5000], [5000, 10000, 5000], [5000, 10000, 800000]]
        rows = []
        for lengths, pressure in zip(steps, [1000., 30000., 50000., 25000., 5000., 500., request["pressure"]]):
            rows += [segment(lengths[0], 600), segment(lengths[1], request["temp"]), segment(lengths[2], request["temp"], pressure=pressure)]
        return rows
    duration = request["eq_ns"] if stage == "eq3" else request["additional_ns"]
    return [segment(round(duration * 1e6), request["temp"], pressure=request["pressure"])]


def _output_schedule(commands, *, stage, trajectory_steps, bulk_dump_steps):
    """Track actual active outputs across native minimize/undump/recreate."""
    active, definitions, records = {}, {}, []
    runs, timestep, thermo = 0, None, None
    custom_fields = ["id", "type", "mol", "x", "y", "z", "ix", "iy", "iz", "vx", "vy", "vz"]
    for line in commands:
        words = line.split()
        if words[0] == "timestep":
            timestep = _finite(words[1])
        elif words[0] == "thermo":
            thermo = int(words[1])
        elif words[0] == "dump":
            if len(words) < 6 or words[3] not in {"xtc", "custom"} or words[1] in active:
                raise RuntimeError("unsupported or already active generated dump identity")
            try:
                cadence = int(words[4])
            except ValueError as error:
                raise RuntimeError("generated dump cadence must be a positive literal integer") from error
            if cadence <= 0 or Path(words[5]).name != words[5]:
                raise RuntimeError("generated dump requires a positive cadence and local file")
            expected = trajectory_steps if words[3] == "xtc" else bulk_dump_steps
            if expected is not None and (expected == 0 or cadence != expected):
                raise RuntimeError("generated trajectory or bulk dump cadence differs from protocol")
            if words[2] != "all" or (words[3] == "xtc" and len(words) != 6):
                raise RuntimeError("generated trajectory group or fields differ from protocol")
            if words[3] == "custom" and bulk_dump_steps is not None and words[6:] != custom_fields:
                raise RuntimeError("generated bulk dump fields differ from protocol")
            identity = words[1]
            if identity in definitions and definitions[identity] != words:
                raise RuntimeError("generated dump recreation changes its cadence/file/group/fields")
            if any(previous[3] == words[3] and previous != words for previous in definitions.values()):
                raise RuntimeError("generated output format has multiple or changed definitions")
            definitions[identity] = words; active[identity] = words
        elif words[0] == "undump":
            if len(words) != 2 or words[1] not in active:
                raise RuntimeError("generated undump refers to an inactive output")
            del active[words[1]]
        elif words[0] == "dump_modify":
            if (len(words) != 4 or words[1] not in active
                    or words[2:] not in (["unwrap", "yes"], ["sort", "id"])):
                raise RuntimeError("unsupported generated dump modification or cadence override")
        elif words[0] == "minimize":
            if active:
                raise RuntimeError("native minimization must suspend bulk trajectory outputs")
            records.append({"operation": "minimize", "active_outputs": []})
        elif words[0] in {"run", "rerun"}:
            for style, expected in (("xtc", trajectory_steps), ("custom", bulk_dump_steps)):
                selected = [value for value in active.values() if value[3] == style]
                # RNEMD starts its trajectory after the NVT preheat.
                preheat = stage == "tc" and runs == 0 and words[0] == "run" and not selected
                if expected is not None and expected > 0 and not preheat and len(selected) != 1:
                    raise RuntimeError("required generated output is inactive during sampling")
            records.append({"operation": words[0], "steps": int(words[1]) if words[0] == "run" else None,
                "time_step_fs": timestep, "thermo_every_steps": thermo,
                "active_outputs": [{"id": value[1], "style": value[3], "every_steps": int(value[4]), "file": value[5],
                    "sample_interval_ps": int(value[4]) * timestep * .001 if timestep is not None else None}
                                   for value in active.values()]})
            if words[0] == "run": runs += 1
    return records


def validate_input(path: Path, snapshot: dict, *, stage: str, schedule=None,
                   thermo_steps=None, trajectory_steps=None, bulk_dump_steps=None, data_name=None, rerun=False) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("unsafe generated input")
    text = path.read_text(); commands = _commands(text)
    if _one(commands, "units") != ["real"] or _one(commands, "atom_style") != ["full"] or _one(commands, "boundary") != ["p", "p", "p"]:
        raise RuntimeError("generated units/atom style/boundary differ from protocol")
    styles = dict(snapshot["styles"])
    if stage == "eq1":
        styles["pair"] = "lj/cut"
    actual_styles = {}
    for name, expected in styles.items():
        fields = _one(commands, name + "_style")
        if fields[0] != expected:
            raise RuntimeError("generated forcefield style differs: " + name)
        actual_styles[name] = " ".join(fields)
    pair = _one(commands, "pair_style")
    expected_cutoffs = [3.] if stage == "eq1" else [8., 12.]
    if len(pair) != len(expected_cutoffs) + 1 or any(not math.isclose(_finite(value), expected) for value, expected in zip(pair[1:], expected_cutoffs)):
        raise RuntimeError("generated pair cutoffs differ from protocol")
    kspace = _one(commands, "kspace_style")
    if kspace != ["none"] and not (stage != "eq1" and len(kspace) == 2 and kspace[0] == "pppm" and math.isclose(float(kspace[1]), 1e-6)):
        raise RuntimeError("generated KSpace differs from protocol")
    if (stage == "eq1") != (kspace == ["none"]):
        raise RuntimeError("generated KSpace phase differs from protocol")
    if _one(commands, "special_bonds") != ["amber"] or _one(commands, "pair_modify") != ["mix", "arithmetic"]:
        raise RuntimeError("generated special bonds/pair mixing differs from protocol")
    if any(line.split()[0] in {"include", "jump", "shell", "pair_coeff", "bond_coeff", "angle_coeff", "dihedral_coeff", "improper_coeff", "suffix"} for line in commands):
        raise RuntimeError("unsupported generated forcefield override")
    data = _one(commands, "read_data")
    if len(data) != 1 or (data_name is not None and data[0] != data_name) or Path(data[0]).name != data[0]:
        raise RuntimeError("generated input reads an unexpected data file")
    data_evidence = _data_evidence(path.parent / data[0], snapshot)
    rows, timestep, active, thermos = [], None, {}, []
    for line in commands:
        words = line.split()
        if words[0] == "timestep":
            timestep = _finite(words[1])
        elif words[0] == "fix" and len(words) > 3 and words[3] in {"nvt", "npt", "nve"}:
            active[words[1]] = words
        elif words[0] == "unfix":
            active.pop(words[1], None)
        elif words[0] == "thermo":
            thermos.append(int(words[1]))
        elif words[0] == "run" and schedule is not None:
            if len(active) != 1 or timestep is None or len(words) != 2:
                raise RuntimeError("ambiguous generated integration schedule")
            fix = next(iter(active.values())); ensemble = fix[3]
            row = {"steps": int(words[1]), "time_step_fs": timestep, "ensemble": ensemble}
            if ensemble in {"nvt", "npt"}:
                t = fix.index("temp"); row.update(temperature_start_k=_finite(fix[t + 1]), temperature_stop_k=_finite(fix[t + 2]))
            if ensemble == "npt":
                if "iso" not in fix:
                    raise RuntimeError("generated pressure must be isotropic")
                p = fix.index("iso"); row.update(pressure_start_atm=_finite(fix[p + 1]), pressure_stop_atm=_finite(fix[p + 2]))
            rows.append(row)
    if schedule is not None and (len(rows) != len(schedule) or any(set(a) != set(b) or any(a[k] != b[k] if isinstance(b[k], str) else not math.isclose(a[k], b[k], rel_tol=0, abs_tol=5.1e-7) for k in b) for a, b in zip(rows, schedule))):
        raise RuntimeError("generated run/temperature/pressure/timestep schedule differs from protocol")
    if thermo_steps is not None and (not thermos or any(value != thermo_steps for value in thermos)):
        raise RuntimeError("generated thermo cadence differs from protocol")
    outputs = _output_schedule(commands, stage=stage, trajectory_steps=trajectory_steps, bulk_dump_steps=bulk_dump_steps)
    if rerun and (any(line.startswith("run ") for line in commands) or [line for line in commands if line.startswith("rerun ")] != ["rerun eq0.dump dump x y z ix iy iz vx vy vz"]):
        raise RuntimeError("generated rerun must re-evaluate the sampled dump without integration")
    return {"schema_version": 1, "verified": True, "stage": stage,
            "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "forcefield": snapshot["forcefield"],
            "styles": actual_styles, "kspace": " ".join(kspace), "special_bonds": "amber", "pair_modify": "mix arithmetic",
            "data": data_evidence, "run_schedule": rows, "thermo_steps": thermo_steps,
            "trajectory_steps": trajectory_steps, "bulk_dump_steps": bulk_dump_steps, "output_schedule": outputs, "rerun": rerun}
