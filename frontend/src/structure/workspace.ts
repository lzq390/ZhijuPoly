import {
  cleanKet, editorLoadCandidates, emptyKet, ketHasAtoms, shouldAdoptEditorSmiles,
  type StructureEditorHandle, type StructureEditorStatus
} from "./editor";
import { retireEditor } from "./editorLifetime";

export type StructureSnapshot = { smiles: string; ket: string | null; revision: number };
export type StructureSyncResult = { status: "saved" | "failed" | "timeout"; message?: string };
export type StructureWorkspaceState = StructureSnapshot & {
  status: StructureEditorStatus;
  draft: string;
  draftError: string | null;
  dirty: boolean;
  notice: string | null;
  mountKey: number;
};
type Standardize = (smiles: string) => Promise<string>;
const deferredDraftNotice = "文本草稿已保留，画板就绪后将继续同步。";
const unsyncedDraftNotice = "文本草稿尚未完成同步，已保留草稿和上次保存的画板。";
const unsyncedDraftWithoutSnapshotNotice = "文本草稿尚未完成同步，草稿已保留；暂无可恢复的画板。";
const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const stale = () => new DOMException("画板操作已失效。", "AbortError");
function bounded<T>(task: Promise<T>, ms: number, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const cleanup = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); };
    const abort = () => { cleanup(); reject(stale()); };
    const timer = setTimeout(() => { cleanup(); reject(new DOMException("画板操作超时。", "TimeoutError")); }, ms);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    task.then((value) => { cleanup(); resolve(value); },
      (error) => { cleanup(); reject(error); });
  });
}

/** One document per App lifetime. Nothing is persisted across users or reloads. */
export class StructureWorkspace {
  private state: StructureWorkspaceState;
  private lastGood: StructureSnapshot | null;
  private listeners = new Set<() => void>();
  private raw: StructureEditorHandle | null = null;
  private retireMount: (() => void) | null = null;
  private owner = 0;
  private epoch = 0;
  private operations = new AbortController();
  private unsubscribeEditor: (() => void) | null = null;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private mutationDepth = 0;
  private externalRevision = 0;
  private pendingSave: { current: () => boolean; revision: number; promise: Promise<StructureSyncResult> } | null = null;

  constructor(initialSmiles = "", private standardize: Standardize = async (value) => value) {
    this.state = {
      smiles: initialSmiles, ket: null, revision: 0, draft: initialSmiles,
      draftError: null, status: "unmounted", dirty: false, notice: null, mountKey: 0
    };
    this.lastGood = initialSmiles ? { smiles: initialSmiles, ket: null, revision: 0 } : null;
  }

  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  private publish(patch: Partial<StructureWorkspaceState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  private stopSave() {
    if (this.saveTimer !== null) clearTimeout(this.saveTimer);
    this.saveTimer = null;
  }
  private expireOperations() {
    ++this.epoch;
    this.pendingSave = null;
    this.operations.abort();
    const retireMount = this.retireMount;
    this.retireMount = null;
    retireMount?.();
    retireEditor(this.raw);
    this.operations = new AbortController();
  }
  private scheduleSave() {
    this.stopSave();
    if (this.mutationDepth || this.state.status !== "ready") return;
    const current = this.guard();
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      void this.saveSnapshot({ reuseSaved: true }).then((result) => {
        if (current() && result.status === "failed" && this.state.status === "ready") {
          this.publish({ notice: "最新画板暂未保存，切页时可能恢复上一次同步的内容。" });
        }
      });
    }, 300);
  }

  /** Capture before starting async work, including text-only validation. */
  guard = () => {
    const owner = this.owner;
    const epoch = this.epoch;
    return () => owner === this.owner && epoch === this.epoch;
  };

  getEditor = (): StructureEditorHandle | null => {
    if (!this.raw || this.state.status !== "ready") return null;
    const raw = this.raw;
    const current = this.guard();
    const signal = this.operations.signal;
    const call = async <T>(action: () => Promise<T>, writesDocument = false) => {
      if (!current()) throw stale();
      let result: T;
      try { result = await bounded(action(), 8000, signal); }
      catch (error) {
        // A timed-out SDK write cannot be cancelled. Retire its actual instance
        // before allowing queued input to proceed, including on the same page.
        if (writesDocument && current() && error instanceof DOMException && error.name === "TimeoutError") {
          this.recoverLastSnapshot("timeout");
        }
        throw error;
      }
      if (!current()) throw stale();
      return result;
    };
    return {
      getSmiles: () => call(() => raw.getSmiles()),
      getKet: () => call(() => raw.getKet()),
      getMolfile: () => call(() => raw.getMolfile()),
      setMolecule: (source) => call(() => raw.setMolecule(source), true),
      clear: () => call(() => raw.clear(), true),
      generateImage: (source, options) => call(() => raw.generateImage(source, options)),
      subscribeChange: (listener) => raw.subscribeChange(() => { if (current()) listener(); }),
      settle: () => call(() => raw.settle()),
      isCurrent: current
    };
  };

  /** A mount lease also fences initialization that resolves after unmount. */
  mountEditor() {
    this.unsubscribeEditor?.();
    this.unsubscribeEditor = null;
    this.stopSave();
    const owner = ++this.owner;
    this.expireOperations();
    this.raw = null;
    this.mutationDepth = 0;
    this.publish({ status: "loading" });
    const current = () => owner === this.owner;
    return {
      // The native provider can already own a Worker before SDK onInit. Keep
      // that initialization under the same immediate navigation fence.
      onRetire: (retire: () => void) => {
        if (current()) this.retireMount = retire;
        else retire();
      },
      initialize: async (editor: StructureEditorHandle, options: { adoptInitialDocument?: boolean } = {}) => {
        if (!current()) return;
        this.raw = editor;
        let restoring = true;
        try {
          const restore = async () => {
            const initial = this.state;
            const external = this.externalRevision;
            let importedRevision: number | undefined;
            if (options.adoptInitialDocument && initial.revision === 0 && !initial.smiles && !initial.ket && !initial.draft) {
              const document = JSON.parse(await editor.getKet());
              const smiles = (await editor.getSmiles()).trim();
              if (!Array.isArray(document?.root?.nodes)) throw new Error("初始导入未返回有效的 KET 文档。");
              if (current() && restoring && this.state === initial && this.externalRevision === external) {
                this.publish({ smiles, draft: smiles, ket: JSON.stringify(cleanKet(document)), revision: initial.revision + 1 });
                importedRevision = this.state.revision;
              }
            }
            // Initial URL imports still receive the same restore/settle checks.
            await this.restore(editor, () => current() && restoring);
            if (current() && restoring && importedRevision !== undefined && this.state.revision === importedRevision) {
              const { smiles, ket, revision } = this.state;
              this.lastGood = { smiles, ket, revision };
            }
          };
          await bounded(restore(), 15000, this.operations.signal);
          if (!current()) return;
          this.unsubscribeEditor = editor.subscribeChange(() => {
            if (!current()) return;
            this.publish({ dirty: true, revision: this.state.revision + 1 });
            this.scheduleSave();
          });
          this.publish({ status: "ready" });
          // Establish a valid empty snapshot on first mount as well.
          this.scheduleSave();
        } catch (error) {
          restoring = false;
          if (current()) {
            retireEditor(editor);
            this.publish({ status: "error", notice: error instanceof Error ? error.message : "画板恢复失败。" });
          }
        }
      },
      fail: (message: string) => {
        if (current()) this.publish({ status: "error", notice: message });
      },
      dispose: () => {
        if (!current()) return;
        this.stopSave();
        this.unsubscribeEditor?.();
        this.unsubscribeEditor = null;
        ++this.owner;
        this.expireOperations();
        this.raw = null;
        this.mutationDepth = 0;
        this.publish({ status: "unmounted" });
      }
    };
  }

  private async restore(editor: StructureEditorHandle, current: () => boolean) {
    while (current()) {
      const { revision, smiles, ket } = this.state;
      if (ket) {
        try {
          await editor.setMolecule(ket);
          if (!current()) return;
          await editor.settle();
          if (!current()) return;
          if (revision !== this.state.revision) continue;
          return;
        } catch {
          if (!current()) return;
          this.publish({ notice: "画板布局恢复失败，正在尝试从 SMILES 恢复结构。" });
        }
      }
      let loaded = !smiles;
      if (!smiles) {
        await editor.clear();
        if (!current()) return;
        await editor.settle();
      } else {
        const candidates = editorLoadCandidates(smiles);
        // An initial import can resolve successfully while still exporting an
        // empty document. Verify and retry once; never accept it as deletion.
        for (let attempt = 0; attempt < candidates.length * 2; ++attempt) {
          const candidate = candidates[Math.floor(attempt / 2)];
          if (!current()) return;
          try {
            await editor.clear();
            if (!current()) return;
            await editor.settle();
            if (!current()) return;
            await editor.setMolecule(candidate);
            if (!current()) return;
            await editor.settle();
            const deadline = Date.now() + 2000;
            do {
              if (!current()) return;
              const exported = (await editor.getSmiles()).trim();
              if (exported && exported !== "{}") { loaded = true; break; }
              if (revision !== this.state.revision) break;
              await pause(80);
            } while (Date.now() < deadline);
            if (loaded) break;
          } catch { /* Try the protected polymer visualization fallback. */ }
        }
      }
      if (!current()) return;
      if (revision !== this.state.revision) continue;
      if (!loaded) throw new Error("共享结构暂时无法恢复到画板，草稿与上次成功快照已保留。");
      if (ket) this.publish({ ket: null });
      return;
    }
  }

  beginMutation() {
    const current = this.guard();
    ++this.mutationDepth;
    this.stopSave();
    let finished = false;
    return () => {
      if (finished || !current()) return;
      finished = true;
      this.mutationDepth = Math.max(0, this.mutationDepth - 1);
      this.scheduleSave();
    };
  }

  /** Internal writes have already been accepted by the editor/standardizer. */
  commitSmiles(value: string, preserveKet = false) {
    const smiles = value.trim();
    const previous = this.state;
    if (smiles === previous.smiles) return;
    this.publish({
      smiles, ket: preserveKet ? previous.ket : null, revision: previous.revision + 1,
      dirty: !preserveKet,
      ...(previous.draft === previous.smiles && !previous.draftError ? { draft: smiles } : {})
    });
    if (this.state.status === "unmounted") {
      this.lastGood = { smiles, ket: this.state.ket, revision: this.state.revision };
    }
  }

  setDraft = (draft: string, draftError: string | null = null) => {
    const clearNotice = draft === this.state.smiles && !draftError &&
      [deferredDraftNotice, unsyncedDraftNotice, unsyncedDraftWithoutSnapshotNotice].includes(this.state.notice || "");
    if (draft.length > 8000 || (draft === this.state.draft && draftError === this.state.draftError && !clearNotice)) return;
    this.publish({ draft, draftError, ...(clearNotice ? { notice: null } : {}) });
  };
  dismissNotice = () => this.publish({ notice: null });

  /** Legacy setSmiles callers (DFT) now write a draft, then an accepted document. */
  setSmiles = (value: string) => {
    if (value.length > 8000) return;
    const request = ++this.externalRevision;
    const revision = this.state.revision;
    // This App-owned request can outlive the text-only page that started it.
    // A newer draft, rather than mounting its destination editor, supersedes it.
    const current = () => request === this.externalRevision && this.state.draft === value && revision === this.state.revision;
    this.setDraft(value);
    void (value.trim() ? this.standardize(value.trim()) : Promise.resolve("")).then((result) => {
      if (!current()) return;
      const smiles = shouldAdoptEditorSmiles(value, result) ? result.trim() : value.trim();
      this.commitSmiles(smiles);
      // A validated external document is already owned by the App, including
      // if its destination is still restoring. Never roll it back to old KET
      // merely because that editor has not completed initialization yet.
      this.lastGood = { smiles: this.state.smiles, ket: this.state.ket, revision: this.state.revision };
      this.setDraft(smiles);
      if (this.raw) this.retryEditor();
    }, () => {
      if (current()) this.setDraft(value, "SMILES 无效或尚未完整，原画板未修改。");
    });
  };

  private savedDocumentIsCurrent() {
    if (this.mutationDepth) return false;
    const { smiles, ket, revision, dirty } = this.state;
    return this.lastGood
      ? this.lastGood.revision === revision && this.lastGood.smiles === smiles && this.lastGood.ket === ket
      : !dirty && !smiles && ket === null;
  }

  saveForNavigation(): Promise<StructureSyncResult> {
    if (this.savedDocumentIsCurrent()) {
      // A restoring editor has no new edits to export. Keep pending text in
      // the App; the destination resumes its usual validated draft pipeline.
      if (this.state.status === "loading" && this.state.draft !== this.state.smiles && !this.state.draftError) {
        this.publish({ notice: deferredDraftNotice });
      }
      return Promise.resolve({ status: "saved" });
    }
    return this.saveSnapshot();
  }

  saveSnapshot({ reuseSaved = false }: { reuseSaved?: boolean } = {}): Promise<StructureSyncResult> {
    this.stopSave();
    // Explicit business exports still read the SDK. Autosave can reuse a full
    // current KET snapshot; a first mount must establish one even when empty.
    if (reuseSaved && this.lastGood?.ket && this.savedDocumentIsCurrent()) {
      return Promise.resolve({ status: "saved" });
    }
    if (this.pendingSave?.current() && this.pendingSave.revision === this.state.revision) return this.pendingSave.promise;
    const pending = { current: this.guard(), revision: this.state.revision, promise: this.captureSnapshot() };
    this.pendingSave = pending;
    void pending.promise.then(() => { if (this.pendingSave === pending) this.pendingSave = null; });
    return pending.promise;
  }

  private async captureSnapshot(): Promise<StructureSyncResult> {
    const current = this.guard();
    try {
      // Let an already running user mutation finish, but navigation owns the
      // overall deadline and expires this work if that deadline is reached.
      while (this.mutationDepth && current()) await pause(40);
      if (!current()) throw stale();
      const editor = this.getEditor();
      if (!editor) {
        if (this.state.status === "unmounted") return { status: "saved" };
        throw new Error("结构编辑器尚未就绪。");
      }
      if (this.state.dirty) await editor.settle();
      for (let attempt = 0; attempt < 8; ++attempt) {
        const revision = this.state.revision;
        const source = this.state.smiles;
        const [rawKet, rawSmiles] = await Promise.all([editor.getKet(), editor.getSmiles()]);
        if (!current()) throw stale();
        if (revision !== this.state.revision || this.mutationDepth) { await pause(80); continue; }
        const parsed = JSON.parse(rawKet) as { root?: { nodes?: unknown[] } };
        if (!Array.isArray(parsed?.root?.nodes)) throw new Error("画板返回了无效的 KET 文档。");
        const exported = rawSmiles.trim() === "{}" ? "" : rawSmiles.trim();
        // Indigo occasionally exposes SMILES before the new KET is committed.
        if ((exported && emptyKet(parsed)) || (!exported && ketHasAtoms(parsed))) { await pause(80); continue; }
        // An empty initial/export response without an edit is not user deletion.
        // In particular, a newly restored editor may still be committing KET.
        if (!exported && emptyKet(parsed) && !this.state.dirty &&
            (source || (this.state.ket && !emptyKet(JSON.parse(this.state.ket))))) {
          await pause(80);
          continue;
        }
        const ket = JSON.stringify(cleanKet(parsed));
        const smiles = exported
          ? shouldAdoptEditorSmiles(source, exported) ? exported : source || exported
          : "";
        const snapshot = { smiles, ket, revision };
        this.lastGood = snapshot;
        this.publish({
          ...snapshot, dirty: false,
          ...(this.state.draft === source && !this.state.draftError ? { draft: smiles } : {})
        });
        return { status: "saved" };
      }
      throw new Error("画板仍在更新，最新快照尚未就绪。");
    } catch (error) {
      return { status: "failed", message: error instanceof Error ? error.message : "画板同步失败。" };
    }
  }

  /** Called only after the navigation save failed/timed out. Never blocks routing. */
  recoverForNavigation(reason: "failed" | "timeout") {
    return this.recoverLastSnapshot(reason);
  }

  private recoverLastSnapshot(reason: "failed" | "timeout") {
    const documentWasSaved = this.savedDocumentIsCurrent();
    const pendingDraft = this.state.draft !== this.state.smiles;
    this.invalidateEditor();
    ++this.externalRevision;
    this.mutationDepth = 0;
    const snapshot = this.lastGood;
    const revision = this.state.revision + 1;
    // Recovery advances the fencing revision, not the document content. The
    // restored snapshot must cover that revision before its new editor loads.
    if (snapshot) this.lastGood = { ...snapshot, revision };
    this.publish({
      ...(snapshot ? { smiles: snapshot.smiles, ket: snapshot.ket } : { smiles: "", ket: null }),
      revision, dirty: false,
      notice: documentWasSaved && pendingDraft
        ? snapshot ? unsyncedDraftNotice : unsyncedDraftWithoutSnapshotNotice
        : snapshot
        ? "最新修改未能同步，将使用上一次保存的画板；未完成的文本草稿已保留。"
        : "最新修改未能同步，暂无可恢复的画板；未完成的文本草稿已保留。",
      // Expire the actual editor too: an uncancellable setMolecule must not
      // later paint into a retained workspace after navigation timed out.
      mountKey: this.state.mountKey + 1
    });
    return { status: reason } satisfies StructureSyncResult;
  }

  retryEditor = () => {
    this.invalidateEditor();
    this.publish({ mountKey: this.state.mountKey + 1 });
  };

  private invalidateEditor() {
    this.stopSave();
    this.unsubscribeEditor?.();
    this.unsubscribeEditor = null;
    ++this.owner;
    this.expireOperations();
    this.raw = null;
    this.mutationDepth = 0;
    this.publish({ status: this.state.status === "unmounted" ? "unmounted" : "loading" });
  }

  getCurrentSmiles = async () => {
    const current = this.guard();
    if (this.state.status === "ready") await this.saveSnapshot();
    const { smiles, revision } = this.state;
    if (!smiles || !current()) return smiles;
    try {
      const standardized = await this.standardize(smiles);
      if (!current() || revision !== this.state.revision) return this.state.smiles;
      const reliable = shouldAdoptEditorSmiles(smiles, standardized) ? standardized : smiles;
      this.commitSmiles(reliable, true);
      return reliable;
    } catch { return this.state.smiles; }
  };
}
