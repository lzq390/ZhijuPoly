export type StructureEditorStatus = "unmounted" | "loading" | "ready" | "error";

export type StructureImageOptions = {
  outputFormat: "png";
  backgroundColor: string;
  "image-resolution": number;
};

/** The only Ketcher capabilities used outside the editor implementation. */
export interface StructureEditorHandle {
  readonly version?: string;
  getSmiles(): Promise<string>;
  getKet(): Promise<string>;
  getMolfile(): Promise<string>;
  setMolecule(source: string): Promise<void>;
  clear(): Promise<void>;
  generateImage(source: string, options: StructureImageOptions): Promise<Blob>;
  subscribeChange(listener: () => void): () => void;
  settle(): Promise<void>;
  isCurrent?(): boolean;
}

/** Structural typing keeps both SDK versions and iframe globals inside adapters. */
export type KetcherInstance = {
  version?: string;
  getSmiles(isExtended?: boolean): Promise<string>;
  getKet?(): Promise<string>;
  getMolfile?(format?: "v2000" | "v3000"): Promise<string>;
  containsReaction?(): boolean;
  structService?: {
    convert(data: { struct: string; input_format?: string; output_format: string }, options?: Record<string, unknown>): Promise<{ struct: string }>;
  };
  setMolecule?(source: string): Promise<unknown>;
  clear?(): unknown;
  generateImage?(source: string, options: StructureImageOptions): Promise<Blob>;
  changeEvent?: { add(listener: () => void): unknown; remove(listener: () => void): unknown };
  formatterFactory?: { create(format: "ket"): { getStructureFromStringAsync(source: string): Promise<{
    initHalfBonds(): void; initNeighbors(): void; setImplicitHydrogen(): void;
    setStereoLabelsToAtoms(): void; markFragments(): void;
  }> } };
  editor?: {
    serverSettings?: Record<string, unknown>;
    struct?(value?: unknown, center?: boolean): unknown;
    zoomAccordingContent?(value: unknown): unknown;
    zoom?(value?: number): number;
    centerStruct?(): void;
    centerViewportAccordingToStruct?(): void;
    render?: {
      options?: { microModeScale?: number };
      clientArea?: { getBoundingClientRect(): { width: number; height: number } };
    };
  };
};

type StructWithBounds = { getCoordBoundingBox?(): { min: { x: number; y: number }; max: { x: number; y: number } } };

// Ketcher's own import fit only ever shrinks (its zoom ceiling is 100%), so a small repeat unit
// lands as a few millimetres in a large canvas. Enlarge imported structures to a readable share
// of the visible canvas, never beyond 2x, and never shrink what Ketcher already fitted.
const IMPORT_FIT_WIDTH = 0.4;
const IMPORT_FIT_HEIGHT = 0.45;
const IMPORT_FIT_MAX_ZOOM = 2;

export function enlargeImportedStructure(ketcher: KetcherInstance) {
  const editor = ketcher.editor;
  const struct = editor?.struct?.() as StructWithBounds | undefined;
  const area = editor?.render?.clientArea?.getBoundingClientRect();
  const scale = editor?.render?.options?.microModeScale;
  if (!editor?.zoom || !struct?.getCoordBoundingBox || !area || !scale || area.width <= 0 || area.height <= 0) return;
  const box = struct.getCoordBoundingBox();
  const width = Math.max(box.max.x - box.min.x, 1);
  const height = Math.max(box.max.y - box.min.y, 1);
  if (!Number.isFinite(width) || !Number.isFinite(height)) return;
  const current = editor.zoom();
  const target = Math.min(
    IMPORT_FIT_MAX_ZOOM,
    (IMPORT_FIT_WIDTH * area.width) / (width * scale),
    (IMPORT_FIT_HEIGHT * area.height) / (height * scale)
  );
  if (!(target > current * 1.05)) return;
  editor.zoom(target);
  editor.centerStruct?.();
}

/**
 * Indigo labels every star atom `star_e` in a CXSMILES atom-label block (`*CC* |$star_e;;;star_e$|`)
 * only to tell it apart from a query "any atom", which it writes as a bare `*`. Indigo reads a bare
 * `*` back as a star atom and RDKit ignores the label, so for repeat-unit connection points it
 * carries nothing but noise. Drop those labels, and the block when nothing else is left in it;
 * every other CX field and label (query atoms, attachment points, enhanced stereo, radicals) stays.
 */
export function dropStarAtomLabels(smiles: string): string {
  const match = /^(\S+)\s+\|(.*)\|\s*$/.exec(smiles.trim());
  if (!match) return smiles;
  const [, body, extension] = match;
  const fields: string[] = [];
  let current = "";
  let inLabels = false;
  for (const char of extension) {
    if (char === "$") inLabels = !inLabels;
    if (char === "," && !inLabels) {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  const kept = fields.flatMap((field) => {
    if (!field.startsWith("$") || field.startsWith("$_AV:") || !field.endsWith("$")) return [field];
    const labels = field.slice(1, -1).split(";").map((label) => (label === "star_e" ? "" : label));
    return labels.some(Boolean) ? [`$${labels.join(";")}$`] : [];
  }).filter(Boolean);
  return kept.length ? `${body} |${kept.join(",")}|` : body;
}

export function createKetcherAdapter(
  ketcher: KetcherInstance,
  refresh?: () => void,
  isCurrent: () => boolean = () => true
): StructureEditorHandle {
  const missing = (method: string): never => { throw new Error(`结构编辑器不支持 ${method}。`); };
  const checkCurrent = () => { if (!isCurrent()) throw new DOMException("画板操作已失效。", "AbortError"); };
  const readMolfile = async () => {
    if (!ketcher.getMolfile) return missing("Molfile 导出");
    // Explicit formats avoid the SDK's stateful automatic-format conversion.
    try { return await ketcher.getMolfile("v2000"); }
    catch (error) { checkCurrent(); return ketcher.getMolfile("v3000"); }
  };
  return {
    version: ketcher.version,
    getSmiles: async () => {
      checkCurrent();
      let value: string;
      if (ketcher.structService && ketcher.getMolfile && !ketcher.containsReaction?.()) {
        // Indigo's KET→SMILES path can lose implicit H at tetrahedral carbon
        // next to a wildcard/query atom. The same native canvas's Molfile
        // preserves H and stereo; convert it with the existing local service.
        const molfile = await readMolfile();
        checkCurrent();
        if (!/V2000|V3000/.test(molfile) || !/^M  END\s*$/m.test(molfile)) {
          throw new Error("结构编辑器未返回有效的 Molfile，已保留上次成功的内容。");
        }
        // A valid empty canvas needs no conversion and remains valid SMILES.
        const empty = /^\s*0\s+0\s+.*V2000\s*$/m.test(molfile) || /^M  V30 COUNTS 0 0 /m.test(molfile);
        value = empty ? "" : (await ketcher.structService.convert({ struct: molfile,
          input_format: "chemical/x-mdl-molfile", output_format: "chemical/x-daylight-smiles" }, ketcher.editor?.serverSettings)).struct;
        checkCurrent();
      } else {
        // Older handles and reaction documents retain their supported path.
        value = await ketcher.getSmiles(false);
        checkCurrent();
      }
      const trimmed = value.trim();
      // Never treat a serializer's Molfile/KET response as business SMILES.
      if (/[\r\n]/.test(trimmed) || (trimmed.startsWith("{") && trimmed !== "{}")) {
        throw new Error("结构编辑器未返回有效的 SMILES，已保留上次成功的内容。");
      }
      // Indigo may append an empty CX atom-label block, or one holding only star-atom labels,
      // to wildcard structures. Only that chemically empty content is discarded.
      return dropStarAtomLabels(value);
    },
    getKet: () => ketcher.getKet?.() ?? missing("KET 导出"),
    getMolfile: readMolfile,
    setMolecule: async (source) => {
      // Public setMolecule unconditionally rescales every imported structure.
      // Use the SDK's KET decoder and the instance's normal canvas commit so
      // saved bond lengths, labels and drawing coordinates survive restoration.
      if (source.trim().startsWith("{") && ketcher.formatterFactory && ketcher.editor?.struct) {
        const document = JSON.parse(source);
        if (Array.isArray(document?.root?.nodes)) {
          if (!isCurrent()) throw new DOMException("画板操作已失效。", "AbortError");
          const structure = await ketcher.formatterFactory.create("ket").getStructureFromStringAsync(source);
          if (!isCurrent()) throw new DOMException("画板操作已失效。", "AbortError");
          structure.initHalfBonds();
          structure.initNeighbors();
          structure.setImplicitHydrogen();
          structure.setStereoLabelsToAtoms();
          structure.markFragments();
          ketcher.editor.struct(structure, false);
          ketcher.editor.zoomAccordingContent?.(structure);
          ketcher.editor.centerStruct?.();
          return;
        }
      }
      if (!ketcher.setMolecule) missing("结构导入");
      await ketcher.setMolecule!(source);
      if (isCurrent() && source.trim()) {
        try { enlargeImportedStructure(ketcher); } catch { /* View fit is cosmetic; the structure is already loaded. */ }
      }
    },
    clear: async () => {
      if (ketcher.clear) await ketcher.clear();
      else if (ketcher.setMolecule) await ketcher.setMolecule("");
      else missing("清空");
    },
    generateImage: (source, options) => ketcher.generateImage?.(source, options) ?? missing("图片导出"),
    subscribeChange(listener) {
      // Ketcher 3.8 editor.unsubscribe('change') removes from a different event
      // than subscribe. Use the same underlying event for registration/removal.
      const event = ketcher.changeEvent;
      if (!event) return missing("画板变更订阅");
      event.add(listener);
      return () => { event.remove(listener); };
    },
    async settle() {
      // Indigo export can lag behind setMolecule resolution. Keep the existing
      // bounded commit delay; only the iframe adapter dispatches frame events.
      await new Promise((resolve) => setTimeout(resolve, 80));
      refresh?.();
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
  };
}

export function wildcardCount(value: string) {
  return value.match(/\*/g)?.length ?? 0;
}

export function shouldAdoptEditorSmiles(source: string, editor: string) {
  return Boolean(editor.trim()) && wildcardCount(editor) >= wildcardCount(source);
}

export function editorLoadCandidates(source: string) {
  const trimmed = source.trim();
  return [...new Set([trimmed, trimmed.replace(/\*/g, "")])].filter(Boolean);
}

export function cleanKet(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cleanKet);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).filter(([key]) => key !== "selected")
    .map(([key, nested]) => [key, cleanKet(nested)]));
}

export function emptyKet(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const root = (value as { root?: { nodes?: unknown[]; connections?: unknown[] } }).root;
  return Boolean(root && Array.isArray(root.nodes) && root.nodes.length === 0 &&
    (!Array.isArray(root.connections) || root.connections.length === 0));
}

export function ketHasAtoms(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some(ketHasAtoms);
  const record = value as Record<string, unknown>;
  return (Array.isArray(record.atoms) && record.atoms.length > 0) || Object.values(record).some(ketHasAtoms);
}
