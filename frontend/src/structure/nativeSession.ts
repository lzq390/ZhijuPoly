import { createKetcherAdapter, emptyKet, type KetcherInstance, type StructureEditorHandle } from "./editor";
import { registerEditorRetirement } from "./editorLifetime";

type OwnedService = { disposed?: boolean; destroy(reason?: Error): void };
export type NativeKetcher = KetcherInstance & {
  nexpolyInitialMol?: string;
  disposed?: boolean;
  retire(): void;
  structService?: { convert(data: { struct: string; output_format: string }): Promise<{ struct: string }> };
  eventBus: { on(event: string, listener: () => void): unknown; off(event: string, listener: () => void): unknown };
};
const aborted = () => new DOMException("画板会话已结束。", "AbortError");
function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(aborted());
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    task.then(value => { signal.removeEventListener("abort", abort); resolve(value); },
      error => { signal.removeEventListener("abort", abort); reject(error); });
  });
}
let nextSession = 0;

/** Owns SDK work even during initialization, before a business handle exists. */
export class NativeEditorSession {
  readonly id = `structure-${++nextSession}`;
  readonly controller = new AbortController();
  private services = new Set<OwnedService>();
  private ketcher: NativeKetcher | null = null;
  private generation = 0;
  private disposers = new Set<() => void>();
  initialized = false;

  constructor(private readonly onFatal: (message: string) => void) {}
  get signal() { return this.controller.signal; }
  check = () => { if (this.signal.aborted) throw aborted(); };

  ownService<T>(service: T): T {
    const owned = service as T & OwnedService;
    if (typeof owned.destroy !== "function") throw new Error("结构服务补丁未生效，请重新安装依赖。");
    if (this.signal.aborted) { owned.destroy(aborted()); throw aborted(); }
    this.services.add(owned);
    return service;
  }

  addCleanup(cleanup: () => void) {
    if (this.signal.aborted) cleanup();
    else this.disposers.add(cleanup);
  }

  fail(message: string) {
    if (this.signal.aborted) return;
    this.dispose();
    this.onFatal(message);
  }

  dispose = () => {
    if (this.signal.aborted) return;
    ++this.generation;
    this.controller.abort();
    // SDK retirement fences writes now. Its React cleanup releases roots later.
    this.ketcher?.retire();
    this.ketcher = null;
    this.services.forEach(service => service.destroy(aborted()));
    this.services.clear();
    this.disposers.forEach(cleanup => cleanup());
    this.disposers.clear();
  };

  attach(ketcher: NativeKetcher): StructureEditorHandle {
    if (this.signal.aborted) { ketcher.retire(); throw aborted(); }
    this.ketcher = ketcher;
    this.initialized = true;
    const generation = this.generation;
    const check = () => {
      this.check();
      if (generation !== this.generation || ketcher.disposed) throw aborted();
    };
    const base = createKetcherAdapter(ketcher, undefined, () => !this.signal.aborted && generation === this.generation && !ketcher.disposed);
    const call = async <T>(action: () => Promise<T>) => {
      check();
      try {
        // Some SDK methods swallow parse/serialization exceptions and resolve.
        // Capture their FAILURE event as well as validating returned values.
        let failed = false;
        const failure = () => { failed = true; };
        ketcher.eventBus.on("FAILURE", failure);
        try {
          const result = await abortable(action(), this.signal);
          check();
          if (failed) throw new Error("结构编辑器未能完成操作，原草稿与有效快照已保留。");
          return result;
        } finally { ketcher.eventBus.off("FAILURE", failure); }
      } catch (error) {
        if (!this.signal.aborted && [...this.services].some(service => service.disposed)) {
          this.fail("结构服务已停止，草稿与上次成功快照已保留，请重试加载画板。");
        }
        throw error;
      }
    };
    const ket = async () => {
      const value = await base.getKet();
      if (!Array.isArray(JSON.parse(value)?.root?.nodes)) throw new Error("结构编辑器未返回有效的 KET 文档。");
      return value;
    };
    const handle: StructureEditorHandle = {
      getSmiles: () => call(base.getSmiles),
      getKet: () => call(ket),
      getMolfile: () => call(async () => {
        const value = await base.getMolfile();
        if (!/V2000|V3000/.test(value)) throw new Error("结构编辑器未返回有效的 Molfile。");
        return value;
      }),
      setMolecule: source => call(async () => {
        await base.setMolecule(source);
        check();
        const document = JSON.parse(await ket());
        check();
        // An explicit empty import is valid; swallowed nonempty imports are not.
        const expectsEmpty = !source.trim() || (source.trim().startsWith("{") && emptyKet(JSON.parse(source)));
        if (emptyKet(document) && !expectsEmpty) throw new Error("结构导入没有生成有效画板。");
      }),
      clear: () => call(async () => {
        await base.clear();
        check();
        if (!emptyKet(JSON.parse(await ket()))) throw new Error("画板尚未完成清空。");
      }),
      generateImage: async (source, options) => {
        let blob: Blob;
        try { blob = await call(() => base.generateImage(source, options)); }
        catch (error) {
          check();
          // Indigo 3.8 cannot render wildcard KET atoms (query type 0), but
          // renders their Molfile correctly. Convert the captured source, never
          // the current canvas, so a late export cannot use another revision.
          const document = source.trim().startsWith("{") ? JSON.parse(source) : null;
          if (!/Query atom type 0 not supported/.test(String(error)) || !ketcher.structService || !Array.isArray(document?.root?.nodes) ||
              document.root.nodes.some((node: { $ref?: string }) => !node.$ref || document[node.$ref]?.type !== "molecule")) throw error;
          const service = ketcher.structService;
          const mol = await call(() => service.convert({ struct: source, output_format: "chemical/x-mdl-molfile" }));
          if (!/V2000|V3000/.test(mol.struct)) throw error;
          blob = await call(() => base.generateImage(mol.struct, options));
        }
        return call(async () => {
          if (!(blob instanceof Blob) || blob.type !== "image/png" || blob.size < 8) throw new Error("结构编辑器未返回有效的 PNG 图片。");
          const signature = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
          if (signature.join() !== "137,80,78,71,13,10,26,10") throw new Error("结构图片格式校验失败。");
          return blob;
        });
      },
      subscribeChange(listener) {
        check();
        const unsubscribe = base.subscribeChange(() => { if (!ketcher.disposed) listener(); });
        return unsubscribe;
      },
      settle: () => call(base.settle),
      isCurrent: () => !this.signal.aborted && generation === this.generation && !ketcher.disposed
    };
    registerEditorRetirement(handle, this.dispose);
    return handle;
  }
}
