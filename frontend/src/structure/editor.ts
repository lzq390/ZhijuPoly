export type StructureEditorStatus = "unmounted" | "loading" | "ready" | "error";

export type StructureImageOptions = {
  outputFormat: "png";
  backgroundColor: string;
  "image-resolution": number;
};

/** The only Ketcher capabilities used outside the editor implementation. */
export interface StructureEditorHandle {
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
    struct?(value: unknown, center?: boolean): unknown;
    zoomAccordingContent?(value: unknown): unknown;
    centerStruct?(): void;
    centerViewportAccordingToStruct?(): void;
  };
};

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
      // Indigo may append an entirely empty CX atom-label block to wildcard
      // structures. Only that chemically empty suffix is discarded.
      const trimmed = value.trim();
      // Never treat a serializer's Molfile/KET response as business SMILES.
      if (/[\r\n]/.test(trimmed) || (trimmed.startsWith("{") && trimmed !== "{}")) {
        throw new Error("结构编辑器未返回有效的 SMILES，已保留上次成功的内容。");
      }
      return value.replace(/\s+\|\$;*\$\|\s*$/, "");
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
