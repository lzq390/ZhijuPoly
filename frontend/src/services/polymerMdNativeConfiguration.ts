import specification from "../../../polymer_md/user_configuration.json";
import type { PolymerMdForcefield, PolymerMdNativeConfiguration, PolymerMdModelConfiguration, PolymerMdRequest } from "../types/polymerMd";

export const POLYMER_MD_NATIVE_FIELDS = ["opt_method", "opt_basis", "charge", "charge_method", "charge_basis",
  "sp_method", "sp_basis", "polar_method", "polar_basis", "properties"] as const;
export const POLYMER_MD_FORCEFIELDS: readonly PolymerMdForcefield[] = ["GAFF", "GAFF2", "GAFF2_mod", "Dreiding", "Dreiding_UT"];
export const POLYMER_MD_MODEL_MODES = ["homopolymer", "alternating", "random", "block", "blend"] as const;

/** SP is an intermolecular rerun and cannot be defined for a one-chain cell. */
export function validPolymerMdPostEquilibriumRequest(request: PolymerMdRequest): boolean {
  const targets = request.native_configuration?.properties;
  return !Array.isArray(targets) || !targets.some(key => key === "sp_total" || key === "sp_ced")
    || Number.isInteger(request.n_chain) && request.n_chain >= 2;
}

const object = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));
const exactKeys = (value: Record<string, unknown>, allowed: readonly string[]) => Object.keys(value).every(key => allowed.includes(key));
const integer = (value: unknown, min: number, max: number): value is number => typeof value === "number"
  && Number.isInteger(value) && value >= min && value <= max;
const repeatSmiles = (value: unknown): value is string => typeof value === "string" && value.length >= 3
  && value.length <= 2048 && /^[\x21-\x7e]+$/.test(value) && (value.match(/\*/g)?.length ?? 0) === 2 && !/[.,]/.test(value);
function validChainLength(value: unknown): boolean {
  if (!object(value) || Object.keys(value).length !== 2 || !exactKeys(value, ["mode", "value"])) return false;
  if (value.mode === "atoms") return integer(value.value, 100, 5000);
  if (value.mode === "dp") return integer(value.value, 1, 5000);
  return value.mode === "molecular_weight" && typeof value.value === "number" && Number.isFinite(value.value)
    && value.value >= 100 && value.value <= 1_000_000;
}

/** Validate only explicit new model data; omitted historical configurations stay omitted. */
export function validPolymerMdModelConfiguration(value: unknown): value is PolymerMdModelConfiguration {
  if (!object(value) || !exactKeys(value, ["version", "mode", "components", "chain_length", "ratios", "blocks"])
    || value.version !== 1 || !(POLYMER_MD_MODEL_MODES as readonly unknown[]).includes(value.mode)
    || !Array.isArray(value.components) || value.components.length < 1 || value.components.length > 4) return false;
  const components = value.components;
  if (!components.every(item => object(item) && exactKeys(item, ["id", "smiles", "n_chain", "chain_length"])
    && typeof item.id === "string" && /^[A-Za-z][A-Za-z0-9_-]{0,15}$/.test(item.id) && repeatSmiles(item.smiles))) return false;
  const identities = components.map(item => (item as Record<string, unknown>).id);
  if (new Set(identities).size !== identities.length || (value.mode === "homopolymer" ? identities.length !== 1 : identities.length < 2)) return false;
  if (value.mode === "blend") return !Object.hasOwn(value, "chain_length") && !Object.hasOwn(value, "ratios") && !Object.hasOwn(value, "blocks")
    && components.every(item => integer(item.n_chain, 1, 20) && validChainLength(item.chain_length))
    && components.reduce((sum, item) => sum + Number(item.n_chain), 0) <= 20;
  if (components.some(item => Object.hasOwn(item, "n_chain") || Object.hasOwn(item, "chain_length"))) return false;
  if (value.mode === "block") {
    if (Object.hasOwn(value, "chain_length") || Object.hasOwn(value, "ratios") || !Array.isArray(value.blocks)
      || value.blocks.length < 2 || value.blocks.length > 16) return false;
    return value.blocks.every(item => object(item) && Object.keys(item).length === 2 && exactKeys(item, ["component_id", "dp"])
      && identities.includes(item.component_id) && integer(item.dp, 1, 5000))
      && new Set(value.blocks.map(item => item.component_id)).size === identities.length;
  }
  if (Object.hasOwn(value, "blocks") || !validChainLength(value.chain_length)) return false;
  if (value.mode !== "random") return !Object.hasOwn(value, "ratios");
  return Array.isArray(value.ratios) && value.ratios.length === identities.length
    && value.ratios.every(item => typeof item === "number" && Number.isFinite(item) && item > 0 && item < 1)
    && Math.abs(value.ratios.reduce((sum: number, item: number) => sum + item, 0) - 1) <= 1e-8;
}

export function validPolymerMdModelRequest(request: PolymerMdRequest): boolean {
  const model = request.model_configuration;
  const targets = request.native_configuration?.properties;
  return !Object.hasOwn(request, "model_configuration") || validPolymerMdModelConfiguration(model)
    && request.smiles === model.components[0].smiles
    && !(Array.isArray(targets) && targets.length > 0 && targets.every(key => typeof key === "string" && key.startsWith("qm_")))
    && (model.mode !== "blend" || request.n_chain === model.components.reduce((sum, component) => sum + component.n_chain!, 0));
}

/** Non-atom targets require chemistry-aware sizing on the backend before admission. */
export function estimatePolymerMdRequestedAtoms(request: PolymerMdRequest): number | null {
  const model = request.model_configuration;
  if (!model) return request.n_atom * request.n_chain;
  if (!validPolymerMdModelRequest(request)) return null;
  if (model.mode === "blend") return model.components.every(component => component.chain_length?.mode === "atoms")
    ? model.components.reduce((sum, component) => sum + component.n_chain! * component.chain_length!.value, 0) : null;
  return model.chain_length?.mode === "atoms" ? model.chain_length.value * request.n_chain : null;
}

/** Validate the exact executable allowlist shared with the server. */
export function validPolymerMdNativeConfiguration(value: unknown): value is PolymerMdNativeConfiguration {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const configuration = value as Record<string, unknown>;
  if (Object.keys(configuration).length !== POLYMER_MD_NATIVE_FIELDS.length
    || Object.keys(configuration).some(key => !(POLYMER_MD_NATIVE_FIELDS as readonly string[]).includes(key))) return false;
  return POLYMER_MD_NATIVE_FIELDS.every(key => {
    const field = specification.fields.find(item => item.id === key);
    if (!field) return false;
    const selected = configuration[key];
    if (key !== "properties") return typeof selected === "string" && field.options.some(option => option.value === selected);
    return Array.isArray(selected) && selected.length > 0 && new Set(selected).size === selected.length
      && selected.every(item => typeof item === "string" && field.options.some(option => option.value === item));
  });
}
