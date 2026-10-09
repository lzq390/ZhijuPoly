import { useEffect, useRef, useState } from 'react';
import { Library, Save } from 'lucide-react';
import type { StructureWorkspace } from '../structure/workspace';
import type { StructureCanvasController } from '../components/structure-workbench/StructureCanvasSurface';
import { getSessionEpoch } from '../auth/session';
import { LibraryModal, useStructureLibrary } from './StructureLibraryProvider';
import type { StructureDocument } from './types';

export function CanvasLibraryActions({ workspace, canvas, disabled }: { workspace: StructureWorkspace; canvas: StructureCanvasController; disabled: boolean }) {
  const library = useStructureLibrary();
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<StructureDocument | null>(null);
  const [fallback, setFallback] = useState(false);
  const alive = useRef(true);
  const epoch = useRef(getSessionEpoch()).current;
  const current = () => alive.current && epoch === getSessionEpoch();
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function save() {
    if (!library || busy) return false;
    setBusy(true);
    try {
      const document = await canvas.captureDocument();
      if (!current()) return false;
      const saved = await library.save(document);
      if (current() && saved) canvas.setFeedback('结构已保存到“我的结构”。');
      return saved && current();
    } catch (error) { if (current()) canvas.setFeedback((error as Error).message); return false; }
    finally { if (current()) setBusy(false); }
  }
  async function restore(document: StructureDocument, smilesOnly = false) {
    setBusy(true);
    try {
      await canvas.restoreDocument(document, undefined, smilesOnly);
      if (current()) { setPending(null); setFallback(false); }
    } catch (error) {
      if (current()) { canvas.setFeedback((error as Error).message); setPending(document); setFallback(true); }
    } finally { if (current()) setBusy(false); }
  }
  async function pick() {
    if (!library) return;
    const selected = await library.pick('载入画板');
    if (!selected || !current() || selected.source.kind === 'canvas') return;
    // Flush edit notifications before comparing full documents. In particular,
    // iframe initialization can report an edit on a still-empty drawing.
    const backup = await workspace.saveSnapshot();
    if (!current()) return;
    if (backup.status !== 'saved') { canvas.setFeedback(backup.message || '无法备份当前画板，未替换结构。'); return; }
    if (workspace.hasUnrecordedChanges()) { setPending(selected.document); setFallback(false); }
    else await restore(selected.document);
  }
  return <>
    <button type="button" className="np-sw-tool" data-workbench-tool="save-record" disabled={disabled || busy || !library} onClick={() => void save()}><Save aria-hidden="true" /><span>保存结构</span></button>
    <button type="button" className="np-sw-tool" data-workbench-tool="structure-library" disabled={busy || !library} onClick={() => void pick()}><Library aria-hidden="true" /><span>我的结构</span></button>
    {pending && <LibraryModal title={fallback ? '恢复结构布局失败' : '载入已保存结构'} onClose={() => { if (!busy) setPending(null); }}>
      <div className="np-library-body"><p>{fallback ? '原画板已保留。可仅按 SMILES 载入此结构，绘图布局将重新生成。' : '当前画板或文本草稿还有尚未保存到结构库的变化。'}</p></div>
      <footer className="np-library-footer"><div className="np-library-footer-actions">
        <button type="button" className="np-library-button np-library-button-secondary" disabled={busy} onClick={() => setPending(null)}>取消</button>
        <button type="button" className={`np-library-button np-library-button-${fallback ? 'primary' : 'secondary'}`} disabled={busy || !canvas.isEditorReady} onClick={() => void restore(pending, fallback)}>{fallback ? '仅按 SMILES 载入' : '直接载入'}</button>
        {!fallback && <button type="button" className="np-library-button np-library-button-primary" disabled={busy} onClick={async () => { const document = pending; setPending(null); if (await save()) await restore(document); else if (current()) setPending(document); }}>先保存再载入</button>}
      </div></footer>
    </LibraryModal>}
  </>;
}
