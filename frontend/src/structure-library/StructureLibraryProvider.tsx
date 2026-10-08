import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import { getSessionEpoch, onSessionRetired } from '../auth/session';
import { requestServiceAccess } from '../auth/guestAccess';
import { ModalFocusSuspendedContext, useModalFocus } from '../hooks/useModalFocus';
import type { StructureWorkspace } from '../structure/workspace';
import { StructureSvg } from '../components/StructureSvg';
import { fetchStructure2D } from '../services/api';
import { structureLibraryApi as api } from './api';
import { recordDocument, sourceLabel, type ImportedStructure, type StructureDocument, type StructureRecordSummary } from './types';
import './structure-library.css';

type PickRequest = { label: string; resolve: (result: ImportedStructure | null) => void };
type SaveRequest = { document: StructureDocument; resolve: (saved: boolean) => void };
type Library = {
  pick: (label: string) => Promise<ImportedStructure | null>;
  save: (document: StructureDocument) => Promise<boolean>;
  drafts: Map<string, unknown>;
  holdModal: () => () => void;
};
const Context = createContext<Library | null>(null);
export const useStructureLibrary = () => useContext(Context);

export function LibraryModal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const holdModal = useStructureLibrary()?.holdModal;
  useLayoutEffect(() => holdModal?.(), [holdModal]);
  return <ModalFocusSuspendedContext.Provider value={false}>
    <LibraryModalContent title={title} onClose={onClose}>{children}</LibraryModalContent>
  </ModalFocusSuspendedContext.Provider>;
}

function LibraryModalContent({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLElement>(null);
  const origin = useRef(document.activeElement as HTMLElement | null);
  useModalFocus({ active: true, open: true, scopeRef: root, panelRef: panel, onClose });
  useEffect(() => () => { const target = origin.current; requestAnimationFrame(() => target?.isConnected && target.focus()); }, []);
  return createPortal(<div className="np-library-overlay" ref={root}>
    <section role="dialog" aria-modal="true" aria-label={title} className="np-library-dialog" ref={panel}>
      <header><h2>{title}</h2><button type="button" onClick={onClose} aria-label="关闭结构窗口">关闭</button></header>
      {children}
    </section>
  </div>, document.body);
}

export function StructureLibraryProvider({ workspace, children }: { workspace: StructureWorkspace; children: ReactNode }) {
  const [picker, setPicker] = useState<PickRequest | null>(null);
  const [saving, setSaving] = useState<SaveRequest | null>(null);
  const pickerRef = useRef<PickRequest | null>(null);
  const savingRef = useRef<SaveRequest | null>(null);
  const [drafts] = useState(() => new Map<string, unknown>());
  const [modalCount, setModalCount] = useState(0);
  const holdModal = useCallback(() => {
    setModalCount(count => count + 1);
    return () => setModalCount(count => count - 1);
  }, []);
  const closePick = useCallback((result: ImportedStructure | null = null) => {
    pickerRef.current?.resolve(result); pickerRef.current = null; setPicker(null);
  }, []);
  const closeSave = useCallback((saved = false) => {
    savingRef.current?.resolve(saved); savingRef.current = null; setSaving(null);
  }, []);
  const pick = useCallback((label: string) => new Promise<ImportedStructure | null>(resolve => {
    closePick(); const next = { label, resolve }; pickerRef.current = next; setPicker(next);
  }), [closePick]);
  const save = useCallback((document: StructureDocument) => new Promise<boolean>(resolve => {
    if (!requestServiceAccess()) { resolve(false); return; }
    closeSave(); const next = { document, resolve }; savingRef.current = next; setSaving(next);
  }), [closeSave]);
  useEffect(() => {
    const retire = () => { closePick(); closeSave(); drafts.clear(); };
    const unsubscribe = onSessionRetired(retire);
    return () => { unsubscribe(); pickerRef.current?.resolve(null); savingRef.current?.resolve(false); drafts.clear(); };
  }, [closePick, closeSave, drafts]);
  return <Context.Provider value={{ pick, save, drafts, holdModal }}>
    <ModalFocusSuspendedContext.Provider value={modalCount > 0}>{children}</ModalFocusSuspendedContext.Provider>
    {picker && <StructurePicker key={picker.label} label={picker.label} workspace={workspace} onClose={closePick} />}
    {saving && <SaveStructureDialog document={saving.document} workspace={workspace} onClose={closeSave} />}
  </Context.Provider>;
}

export function StructurePicker({ label, workspace, onClose }: {
  label: string; workspace: StructureWorkspace; onClose: (value?: ImportedStructure | null) => void;
}) {
  const [tab, setTab] = useState<'saved' | 'canvas'>('saved');
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<StructureRecordSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [edit, setEdit] = useState<{ record: StructureRecordSummary; name: string; action: 'rename' | 'delete' } | null>(null);
  const epoch = useRef(getSessionEpoch()).current;
  const controller = useRef(new AbortController());
  const pageRequest = useRef<AbortController | null>(null);
  const alive = () => !controller.current.signal.aborted && epoch === getSessionEpoch();
  useEffect(() => {
    controller.current = new AbortController();
    return () => { controller.current.abort(); pageRequest.current?.abort(); };
  }, []);
  useEffect(() => {
    if (tab !== 'saved') return;
    const abort = new AbortController(); pageRequest.current?.abort(); pageRequest.current = abort;
    setSelected(null); setItems([]); setCursor(null); setLoading(true); setError(null);
    const timer = setTimeout(() => {
      void api.status(abort.signal).then(status => {
        if (!status.available) throw new Error(status.message || '个人结构库尚未启用。');
        return api.list(query, null, abort.signal);
      }).then(result => { if (!abort.signal.aborted && alive()) { setItems(result.items); setCursor(result.next_cursor); } })
        .catch(reason => { if (!abort.signal.aborted && alive()) setError(reason instanceof Error ? reason.message : '无法加载结构库。'); })
        .finally(() => { if (!abort.signal.aborted && alive()) setLoading(false); });
    }, 180);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [tab, query, reload]);
  async function more() {
    if (!cursor || loading) return;
    setLoading(true);
    const signal = pageRequest.current?.signal;
    try { const result = await api.list(query, cursor, signal); if (alive() && !signal?.aborted) { setItems(old => [...old, ...result.items]); setCursor(result.next_cursor); } }
    catch (reason) { if (alive() && !signal?.aborted) setError((reason as Error).message); }
    finally { if (alive() && !signal?.aborted) setLoading(false); }
  }
  async function confirm() {
    if (!selected || busy) return;
    setBusy(true); setError(null);
    try {
      const result: ImportedStructure = selected === 'canvas'
        ? { source: { kind: 'canvas' }, document: await workspace.captureForImport(controller.current.signal), smiles: '' }
        : await api.detail(selected, controller.current.signal).then(record => ({
          source: { kind: 'saved', id: record.id, name: record.name }, smiles: record.smiles, document: recordDocument(record)
        }));
      result.smiles = result.document.smiles;
      if (alive()) onClose(result);
    } catch (reason) { if (alive()) setError((reason as Error).message); }
    finally { if (alive()) setBusy(false); }
  }
  async function manage() {
    if (!edit || busy) return;
    setBusy(true); setError(null);
    try {
      if (edit.action === 'delete') await api.remove(edit.record.id, controller.current.signal);
      else await api.rename(edit.record, edit.name.trim(), controller.current.signal);
      if (alive()) { setEdit(null); setSelected(null); setReload(value => value + 1); }
    } catch (reason) { if (alive()) setError((reason as Error).message); }
    finally { if (alive()) setBusy(false); }
  }
  return <LibraryModal title={`选择结构 · ${label}`} onClose={() => onClose(null)}>
    <nav className="np-library-tabs" aria-label="结构来源">
      <button type="button" aria-pressed={tab === 'saved'} disabled={busy} onClick={() => { setTab('saved'); setSelected(null); setEdit(null); setError(null); }}>已保存结构</button>
      <button type="button" aria-pressed={tab === 'canvas'} disabled={busy} onClick={() => { setTab('canvas'); setSelected(null); setEdit(null); setError(null); }}>当前画板</button>
    </nav>
    {tab === 'saved' ? <>
      <label className="np-library-search">搜索结构<input aria-label="搜索结构" value={query} onChange={e => setQuery(e.target.value)} maxLength={8000} placeholder="名称或 SMILES" disabled={busy} /></label>
      <div className="np-library-list" role="radiogroup" aria-label="已保存结构">
        {items.map(record => <article key={record.id} className={selected === record.id ? 'is-selected' : ''}>
          <label><input type="radio" name="saved-structure" checked={selected === record.id} onChange={() => setSelected(record.id)} disabled={busy} />
            {record.preview_svg ? <StructureSvg svg={record.preview_svg} alt={`${record.name} 结构预览`} /> : <span>暂无预览</span>}
            <span><strong>{record.name}</strong><code>{record.smiles}</code><small>{new Date(record.created_at).toLocaleString()}</small></span>
          </label>
          <div><button type="button" disabled={busy} onClick={() => setEdit({ record, name: record.name, action: 'rename' })}>重命名</button>
            <button type="button" disabled={busy} onClick={() => setEdit({ record, name: record.name, action: 'delete' })}>删除</button></div>
        </article>)}
        {!loading && !error && !items.length && <p>{query ? '没有匹配的结构。' : '还没有保存的结构。请在画板点击“保存结构”。'}</p>}
        {loading && <p role="status">正在加载结构…</p>}
      </div>
      {cursor && <button type="button" disabled={loading || busy} onClick={() => void more()}>加载更多</button>}
    </> : <label className="np-library-current"><input type="radio" aria-label="使用当前画板" name="saved-structure" checked={selected === 'canvas'} disabled={busy} onChange={() => setSelected('canvas')} />使用当前画板<code>{workspace.getSnapshot().smiles || '画板为空或尚未同步'}</code><small>确认时读取完整结构；读取失败将保留目标输入。</small></label>}
    {edit && <div className="np-library-edit">
      {edit.action === 'rename' ? <label>新名称<input aria-label="结构新名称" maxLength={100} value={edit.name} onChange={e => setEdit({ ...edit, name: e.target.value })} disabled={busy} /></label>
        : <p>删除“{edit.record.name}”？已导入模块的结构不受影响。</p>}
      <button type="button" disabled={busy || !edit.name.trim()} onClick={() => void manage()}>{edit.action === 'delete' ? '确认删除' : '保存名称'}</button>
      <button type="button" disabled={busy} onClick={() => setEdit(null)}>取消修改</button>
    </div>}
    {error && <p role="alert">{error} {tab === 'saved' && <button type="button" disabled={busy} onClick={() => setReload(value => value + 1)}>刷新重试</button>}</p>}
    <footer><button type="button" onClick={() => onClose(null)}>取消</button><button type="button" disabled={!selected || busy || !!edit} onClick={() => void confirm()}>{busy ? '处理中…' : '导入'}</button></footer>
  </LibraryModal>;
}

export function SaveStructureDialog({ document, workspace, onClose }: { document: StructureDocument; workspace: StructureWorkspace; onClose: (saved: boolean) => void }) {
  const [name, setName] = useState(() => '结构 ' + new Date().toLocaleString('sv-SE').slice(0, 16));
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef(new AbortController());
  const epoch = useRef(getSessionEpoch()).current;
  const request = useRef<{ name: string; id: string } | null>(null);
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    void fetchStructure2D(document.smiles, abort.signal).then(result => { if (!abort.signal.aborted && epoch === getSessionEpoch()) setPreview(result.structure_svg); }).catch(() => {});
    return () => abort.abort();
  }, [document, epoch]);
  async function save() {
    if (busy || !name.trim()) return;
    setBusy(true); setError(null);
    if (request.current?.name !== name.trim()) request.current = { name: name.trim(), id: crypto.randomUUID() };
    try {
      const status = await api.status(controller.current.signal);
      if (!status.available) throw new Error(status.message || '个人结构库尚未启用。');
      await api.save(document, request.current.name, request.current.id, controller.current.signal);
      if (!controller.current.signal.aborted && epoch === getSessionEpoch()) { workspace.markRecorded(document); onClose(true); }
    } catch (reason) { if (!controller.current.signal.aborted && epoch === getSessionEpoch()) setError((reason as Error).message); }
    finally { if (!controller.current.signal.aborted && epoch === getSessionEpoch()) setBusy(false); }
  }
  return <LibraryModal title="保存结构" onClose={() => onClose(false)}>
    {preview && <StructureSvg svg={preview} alt="待保存结构预览" className="np-library-save-preview" />}
    <code className="np-library-smiles">{document.smiles}</code>
    <label>结构名称<input aria-label="结构名称" value={name} maxLength={100} disabled={busy} onChange={e => setName(e.target.value)} /></label>
    <p>保存此次完整画板。之后修改画板不会覆盖此记录。</p>
    {error && <p role="alert">{error}</p>}
    <footer><button type="button" onClick={() => onClose(false)}>取消</button><button type="button" disabled={busy || !name.trim()} onClick={() => void save()}>{busy ? '保存中…' : '确认保存'}</button></footer>
  </LibraryModal>;
}

/** Module drafts survive navigation, but never survive identity retirement. */
export function useModuleDraftState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const library = useStructureLibrary();
  const epoch = useRef(getSessionEpoch()).current;
  const [value, setValue] = useState<T>(() => library?.drafts.has(key) ? library.drafts.get(key) as T : typeof initial === 'function' ? (initial as () => T)() : initial);
  useEffect(() => { if (epoch === getSessionEpoch()) library?.drafts.set(key, value); }, [library?.drafts, key, value, epoch]);
  return [value, setValue];
}

/** The target value and component lifetime fence late picker results. */
export function useStructureImport(value: string, onSelect: (smiles: string, selection: ImportedStructure) => void, label: string) {
  const library = useStructureLibrary();
  const state = useRef({ value, revision: 0, mounted: true });
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  if (state.current.value !== value) { state.current.value = value; state.current.revision++; }
  const [source, setSource] = useState<ImportedStructure | null>(null);
  useEffect(() => { state.current.mounted = true; return () => { state.current.mounted = false; }; }, []);
  useEffect(() => { if (source && value !== source.smiles) setSource(null); }, [value, source]);
  async function choose() {
    if (!library) return null;
    const revision = ++state.current.revision;
    const epoch = getSessionEpoch();
    const selected = await library.pick(label);
    if (!selected || !state.current.mounted || revision !== state.current.revision || epoch !== getSessionEpoch()) return null;
    setSource(selected); onSelectRef.current(selected.smiles, selected);
    return selected;
  }
  return { choose, source: source && value === source.smiles ? sourceLabel(source) : null };
}

export function StructureImportButton({ value, onChange, label = '结构', disabled = false }: { value: string; onChange: (smiles: string) => void; label?: string; disabled?: boolean }) {
  const selection = useStructureImport(value, onChange, label);
  return <span className="np-library-import"><button type="button" disabled={disabled} aria-label={`选择结构 · ${label}`} onClick={() => void selection.choose()}>选择结构</button>{selection.source && <small>来自：{selection.source}</small>}</span>;
}
