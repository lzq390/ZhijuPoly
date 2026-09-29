import { lazy, Suspense, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { getSessionEpoch } from "./session";
import { StructureWorkspace } from "../structure/workspace";
const Editor = lazy(() => import("../components/structure-workbench/StructureEditor").then(module => ({ default: module.StructureEditor })));
const TEMPLATES = [
  { name: "乙醇", smiles: "CCO" },
  { name: "苯", smiles: "c1ccccc1" },
  { name: "乙烯", smiles: "C=C" }
];
export function GuestWorkspace({ onDraft, onCapture, initialSmiles = "" }: { initialSmiles?: string; onDraft: (smiles: string) => void; onCapture?: (capture: (() => Promise<string>) | null) => void }) {
  const identityEpoch = useRef(getSessionEpoch()).current;
  // The identity standardizer and standalone editor only transform data locally.
  const [workspace] = useState(() => new StructureWorkspace(initialSmiles));
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    onCapture?.(async () => { await workspace.getCurrentSmiles(); return workspace.getSnapshot().draft; });
    return () => onCapture?.(null);
  }, [onCapture, workspace]);
  async function copy() {
    const smiles = await workspace.getCurrentSmiles();
    if (identityEpoch !== getSessionEpoch()) return;
    onDraft(smiles);
    try { await navigator.clipboard.writeText(smiles); setNotice("已复制 SMILES"); }
    catch { setNotice("请从文本框选择并复制 SMILES"); }
  }
  return <section className="np-auth-guest" aria-label="游客体验">
    <h1 id="guest-introduction">NexPoly · 科研工作空间</h1>
    <p>浏览产品介绍，使用本地画板探索分子结构。登录后可使用数据库、文献检索、AI 助手及科学计算，并管理自己的任务与结果。</p>
    <div className="np-auth-features"><span>结构设计</span><span>性质预测</span><span>计算模拟</span><span>文献分析</span></div>
    <h2 id="guest-local-workspace">本地结构体验</h2>
    <p>以下简单分子作为绘图示例，所有编辑和复制均在浏览器内完成。</p>
    <div className="np-auth-actions">{TEMPLATES.map(item => <button key={item.smiles} type="button" onClick={() => {
      workspace.setSmiles(item.smiles); onDraft(item.smiles); setOpen(true);
    }}>{item.name}</button>)}<button type="button" onClick={() => setOpen(true)}>打开本地画板</button></div>
    {open && <div className="np-auth-canvas"><Suspense fallback={<p role="status">正在加载本地画板…</p>}><Editor workspace={workspace} title="游客本地画板" /></Suspense></div>}
    <label>SMILES<textarea aria-label="游客 SMILES" value={state.draft} maxLength={8000} onChange={event => {
      workspace.setDraft(event.target.value); onDraft(event.target.value);
    }} /></label>
    <div className="np-auth-actions"><button type="button" onClick={() => { workspace.setSmiles(state.draft); setOpen(true); }}>同步到画板</button><button type="button" onClick={() => void copy()}>复制 SMILES</button><button type="button" onClick={() => void workspace.getCurrentSmiles().then(smiles => { if (identityEpoch !== getSessionEpoch()) return; onDraft(smiles); setNotice("已保存本次游客画板，登录后可选择导入。"); })}>保存本次游客画板</button></div>
    {notice && <p role="status">{notice}</p>}
    <h2 id="guest-batch-templates">批量聚合模板</h2>
    <p>下载公开输入模板，登录后可以上传、预检并提交自己的任务。</p>
    <div className="np-auth-actions">{["a", "b"].flatMap(role => ["csv", "xlsx"].map(format => <a key={`${role}.${format}`} href={`${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/monomer-polymerization/batch/templates/${role}.${format}`} download>单体表 {role.toUpperCase()} · {format.toUpperCase()}</a>))}</div>
  </section>;
}
