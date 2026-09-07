import type { MonomerMdFormalProtocol } from "../../types";

export const FORMAL_PROTOCOLS: MonomerMdFormalProtocol[] = [
  "Density",
  "Transport",
  "HVap",
  "Dielectric",
  "Compressibility"
];

export const MANAGED_PATH_VALUES = {
  params_dir: "managed_params",
  output_dir: "managed_output",
  working_dir: "managed_working"
} as const;

export type FormalComponentRow = {
  id: string;
  name: string;
  ratio: string;
  smiles: string;
};

export type StructuredFormalDraft = {
  temperature: string;
  natoms: string;
  components: FormalComponentRow[];
  nptSteps: string;
  nvtSteps: string;
  dipoleInterval: string;
};

export type FormalConfigValidation = {
  valid: boolean;
  errors: string[];
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function cloneConfig(config: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(config)) as Record<string, unknown>;
}

function finitePositiveNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

export function validateFormalConfig(
  config: unknown,
  protocol: MonomerMdFormalProtocol
): FormalConfigValidation {
  const errors: string[] = [];
  if (!isRecord(config)) {
    return { valid: false, errors: ["配置必须是一个 JSON 对象。"] };
  }
  if (config.protocol !== protocol) {
    errors.push(`protocol 必须固定为 ${protocol}。`);
  }
  if (!finitePositiveNumber(config.temperature)) {
    errors.push("温度必须是有限正数。 ");
  }
  if (!positiveInteger(config.natoms) || config.natoms > 10_000) {
    errors.push("目标原子数必须是 1–10000 的整数。 ");
  }

  const components = config.components;
  const smiles = config.smiles;
  if (!isRecord(components) || Object.keys(components).length === 0) {
    errors.push("components 必须是非空对象。 ");
  }
  if (!isRecord(smiles) || Object.keys(smiles).length === 0) {
    errors.push("smiles 必须是非空对象。 ");
  }
  if (isRecord(components) && isRecord(smiles)) {
    const componentNames = Object.keys(components);
    const smilesNames = Object.keys(smiles);
    if (
      componentNames.length !== smilesNames.length ||
      componentNames.some((name) => !Object.prototype.hasOwnProperty.call(smiles, name))
    ) {
      errors.push("components 与 smiles 必须使用完全相同的组分名称。 ");
    }
    for (const name of componentNames) {
      if (!name.trim()) {
        errors.push("组分名称不能为空。 ");
      }
      if (!finitePositiveNumber(components[name])) {
        errors.push(`组分 ${name || "（未命名）"} 的摩尔配比必须是有限正数。`);
      }
    }
    for (const name of smilesNames) {
      const value = smiles[name];
      if (typeof value !== "string" || !value.trim()) {
        errors.push(`组分 ${name || "（未命名）"} 的 SMILES 不能为空。`);
      } else if (value.includes("*")) {
        errors.push(`组分 ${name} 的 SMILES 不得包含 * 连接点。`);
      }
    }
    if (protocol === "HVap" && componentNames.length !== 1) {
      errors.push("HVap 协议必须恰好包含一个组分。 ");
    }
  }

  if (protocol === "Dielectric") {
    for (const field of ["npt_steps", "nvt_steps", "dipole_interval"] as const) {
      if (field in config && !positiveInteger(config[field])) {
        errors.push(`${field} 存在时必须是正整数。`);
      }
    }
  }
  if (protocol === "Compressibility" && "npt_steps" in config) {
    if (!positiveInteger(config.npt_steps) || config.npt_steps <= 1_000_000) {
      errors.push("Compressibility 的 npt_steps 必须严格大于 1,000,000。 ");
    }
  }

  return { valid: errors.length === 0, errors: [...new Set(errors.map((item) => item.trim()))] };
}

export function parseAndValidateFormalConfig(
  text: string,
  protocol: MonomerMdFormalProtocol
): { config: Record<string, unknown> | null; errors: string[] } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const detail = error instanceof Error ? error.message : "JSON 语法无效";
    return { config: null, errors: [`JSON 无法解析：${detail}`] };
  }
  const validation = validateFormalConfig(parsed, protocol);
  return {
    config: validation.valid && isRecord(parsed) ? parsed : null,
    errors: validation.errors
  };
}

export function restoreManagedPaths(config: Record<string, unknown>): Record<string, unknown> {
  return {
    ...cloneConfig(config),
    ...MANAGED_PATH_VALUES
  };
}

export function estimatedFormalSteps(
  protocol: MonomerMdFormalProtocol,
  config: Record<string, unknown>
): number {
  if (protocol === "Density") return 1_500_000;
  if (protocol === "Transport") return 15_000_000;
  if (protocol === "HVap") return 6_500_000;
  if (protocol === "Dielectric") {
    const npt = positiveInteger(config.npt_steps) ? config.npt_steps : 2_000_000;
    const nvt = positiveInteger(config.nvt_steps) ? config.nvt_steps : 6_000_000;
    return npt + nvt;
  }
  return positiveInteger(config.npt_steps) ? config.npt_steps : 5_000_000;
}

let nextComponentId = 1;

function componentId() {
  const id = nextComponentId;
  nextComponentId += 1;
  return `component-${id}`;
}

export function structuredDraftFromConfig(config: Record<string, unknown>): StructuredFormalDraft {
  const components = isRecord(config.components) ? config.components : {};
  const smiles = isRecord(config.smiles) ? config.smiles : {};
  return {
    temperature: typeof config.temperature === "number" ? String(config.temperature) : "",
    natoms: typeof config.natoms === "number" ? String(config.natoms) : "",
    components: Object.keys(components).map((name) => ({
      id: componentId(),
      name,
      ratio: typeof components[name] === "number" ? String(components[name]) : "",
      smiles: typeof smiles[name] === "string" ? smiles[name] : ""
    })),
    nptSteps: typeof config.npt_steps === "number" ? String(config.npt_steps) : "",
    nvtSteps: typeof config.nvt_steps === "number" ? String(config.nvt_steps) : "",
    dipoleInterval: typeof config.dipole_interval === "number" ? String(config.dipole_interval) : ""
  };
}

function parseOptionalInteger(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : Number.NaN;
}

export function buildConfigFromStructuredDraft(
  base: Record<string, unknown>,
  protocol: MonomerMdFormalProtocol,
  draft: StructuredFormalDraft
): { config: Record<string, unknown> | null; errors: string[] } {
  const names = draft.components.map((row) => row.name.trim());
  if (new Set(names).size !== names.length) {
    return { config: null, errors: ["组分名称必须唯一。"] };
  }

  const next = cloneConfig(base);
  next.protocol = protocol;
  next.temperature = Number(draft.temperature);
  next.natoms = Number(draft.natoms);
  next.components = Object.fromEntries(
    draft.components.map((row) => [row.name.trim(), Number(row.ratio)])
  );
  next.smiles = Object.fromEntries(
    draft.components.map((row) => [row.name.trim(), row.smiles.trim()])
  );

  if (protocol === "Dielectric") {
    const npt = parseOptionalInteger(draft.nptSteps);
    const nvt = parseOptionalInteger(draft.nvtSteps);
    const interval = parseOptionalInteger(draft.dipoleInterval);
    if (npt === undefined) delete next.npt_steps;
    else next.npt_steps = npt;
    if (nvt === undefined) delete next.nvt_steps;
    else next.nvt_steps = nvt;
    if (interval === undefined) delete next.dipole_interval;
    else next.dipole_interval = interval;
  } else if (protocol === "Compressibility") {
    const npt = parseOptionalInteger(draft.nptSteps);
    if (npt === undefined) delete next.npt_steps;
    else next.npt_steps = npt;
  }

  const validation = validateFormalConfig(next, protocol);
  return validation.valid ? { config: next, errors: [] } : { config: null, errors: validation.errors };
}

function sortForFingerprint(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortForFingerprint);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((key) => [key, sortForFingerprint(value[key])])
  );
}

export function configFingerprint(config: Record<string, unknown>): string {
  return JSON.stringify(sortForFingerprint(config));
}

export function configsEqual(
  left: Record<string, unknown>,
  right: Record<string, unknown>
): boolean {
  return configFingerprint(left) === configFingerprint(right);
}
