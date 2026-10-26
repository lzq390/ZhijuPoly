/** Shared analysis performed for every new polymer MD request. */
export const POLYMER_MD_BASE_PROPERTIES = [
  "density", "Rg", "self-diffusion", "Cp", "Cv", "compressibility",
  "isentropic_compressibility", "bulk_modulus", "isentropic_bulk_modulus",
  "volume_expansion", "linear_expansion", "r2", "static_dielectric_const",
  "nematic_order_parameter"
] as const;

/** These four outputs share one molecular single-point calculation. */
export const POLYMER_MD_SINGLE_POINT_PROPERTIES = [
  "qm_total_energy", "qm_homo", "qm_lumo", "qm_dipole"
] as const;

export const POLYMER_MD_POLARIZABILITY_PROPERTY = "qm_polarizability";
export const POLYMER_MD_REFRACTIVE_INDEX_PROPERTY = "refractive_index";
export const POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY = "thermal_conductivity";
export const POLYMER_MD_THERMAL_DIFFUSIVITY_PROPERTY = "thermal_diffusivity";
export const POLYMER_MD_TG_PROPERTY = "tg";
export const POLYMER_MD_GLASS_TRANSITION_PROPERTY = POLYMER_MD_TG_PROPERTY;
/** One molecular-interaction rerun supplies both Hildebrand SP and CED. */
export const POLYMER_MD_SOLUBILITY_PROPERTIES = ["sp_total", "sp_ced"] as const;

/** Compile new form selections only; historical and pending requests stay exact. */
export function expandPolymerMdProperties(selected: readonly string[]): string[] {
  const properties = new Set<string>([...POLYMER_MD_BASE_PROPERTIES, ...selected]);
  if (POLYMER_MD_SINGLE_POINT_PROPERTIES.some(key => properties.has(key))) {
    for (const key of POLYMER_MD_SINGLE_POINT_PROPERTIES) properties.add(key);
  }
  if (properties.has(POLYMER_MD_THERMAL_DIFFUSIVITY_PROPERTY)) properties.add(POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY);
  if (POLYMER_MD_SOLUBILITY_PROPERTIES.some(key => properties.has(key))) {
    for (const key of POLYMER_MD_SOLUBILITY_PROPERTIES) properties.add(key);
  }
  return [...properties].sort();
}
