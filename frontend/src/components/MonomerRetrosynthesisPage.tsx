import { useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { useAuth } from "../auth/AuthProvider";
import { getSession, getSessionEpoch } from "../auth/session";
import { requestServiceAccess } from "../auth/guestAccess";
import { useMonomerRetrosynthesis } from "../hooks/useMonomerRetrosynthesis";
import type { StructureWorkspaceContext } from "../types";
import { ModulePageHeader } from "./ModulePageHeader";
import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { MonomerRetrosynthesisForm } from "./monomer-retrosynthesis/MonomerRetrosynthesisForm";
import { RetrosynthesisDrawer } from "./monomer-retrosynthesis/RetrosynthesisDrawer";
import { readRetrosynthesisDraft, saveRetrosynthesisDraft,
  type MonomerRetrosynthesisDraft, type MonomerRetrosynthesisInput } from "./monomer-retrosynthesis/session";
import "../styles/structure-workbench.css";
import "../styles/monomer-retrosynthesis.css";

type Props = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  initialInput?: MonomerRetrosynthesisInput;
};

export function MonomerRetrosynthesisPage({ structure, onEditStructure, initialInput }: Props) {
  const auth = useAuth();
  const identityEpoch = useRef(getSessionEpoch()).current;
  const mounted = useRef(true);
  const draftRevision = useRef(0);
  const targetRef = useRef<HTMLTextAreaElement | null>(null);
  const [form, setForm] = useState<MonomerRetrosynthesisDraft>(() => {
    const draft = readRetrosynthesisDraft();
    return initialInput?.smiles?.trim() ? { ...draft, smiles: initialInput.smiles.trim() } : draft;
  });
  const [notice, setNotice] = useState<string | null>(initialInput?.notice ?? null);
  const [importing, setImporting] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [selectedCandidateIndex, setSelectedCandidateIndex] = useState(0);
  const [adjusting, setAdjusting] = useState(false);
  const retro = useMonomerRetrosynthesis();
  const current = () => mounted.current && identityEpoch === getSessionEpoch();

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => { saveRetrosynthesisDraft(form, identityEpoch); }, [form, identityEpoch]);

  function updateForm(patch: Partial<MonomerRetrosynthesisDraft>) {
    if (!current()) return;
    draftRevision.current += 1;
    setForm(previous => ({ ...previous, ...patch }));
    setNotice(null);
  }

  async function importSharedStructure() {
    const revision = draftRevision.current;
    setImporting(true);
    try {
      const smiles = (await structure.getCurrentSmiles()).trim();
      if (!current() || revision !== draftRevision.current) return null;
      const state = structure.workspace.getSnapshot();
      if (state.draftError || (state.status !== "unmounted" && state.dirty) || state.draft.trim() !== state.smiles.trim()) {
        setNotice("共享结构尚未完成同步，已保留当前反推目标。请在工作台检查结构后重试。");
        return null;
      }
      if (!smiles) {
        setNotice("当前共享结构为空，已保留反推草稿。");
        return null;
      }
      updateForm({ smiles });
      return smiles;
    } catch {
      if (current() && revision === draftRevision.current) setNotice("读取共享结构失败，已保留反推草稿。请重试。");
      return null;
    } finally { if (current()) setImporting(false); }
  }

  const count = Number(form.returnCount);
  const targetError = form.smiles.trim() ? null : "请输入目标单体的 SMILES。";
  const countError = form.returnCount.trim() && Number.isInteger(count) && count >= 1 && count <= 10
    ? null : "候选数必须是 1–10 的整数。";
  const request = { smiles: form.smiles.trim(), target_role: form.targetRole,
    num_beams: Math.max(5, count), num_return_sequences: count, max_new_tokens: 128 };
  const stale = Boolean(retro.snapshot && (retro.snapshot.smiles !== request.smiles ||
    retro.snapshot.target_role !== request.target_role || retro.snapshot.num_return_sequences !== count));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!current() || !requestServiceAccess()) return;
    setAttempted(true);
    if (targetError || countError) {
      if (targetError) targetRef.current?.focus();
      return;
    }
    if (retro.run(request)) {
      setSelectedCandidateIndex(0);
      setAdjusting(false);
      setDrawerOpen(true);
    }
  }

  return <div className="np-module-page np-structure-workbench np-monomer-retrosynthesis"
    data-module="monomer-retrosynthesis" style={{ "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties}>
    <ModulePageHeader actions={<BrowsingRecordingControls module="monomerRetrosynthesis" />}>单体逆合成反推</ModulePageHeader>
    <div className={`np-sw-page np-module-page-body${drawerOpen ? " has-open-drawer" : ""}`}>
      <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
        <main className="np-sw-workspace">
          <div className="np-mr-content">
            <MonomerRetrosynthesisForm form={form} guest={auth ? auth.status === "guest" : !getSession().authenticated}
              loading={retro.loading} importing={importing} targetRef={targetRef}
              targetError={attempted ? targetError : null} countError={attempted ? countError : null} notice={notice}
              onChange={updateForm} onImport={importSharedStructure} onSubmit={submit} onEdit={() => {
                if (!current()) return;
                saveRetrosynthesisDraft(form, identityEpoch); onEditStructure();
              }} />
          </div>
        </main>
        <RetrosynthesisDrawer open={drawerOpen} hasRun={Boolean(retro.snapshot)} width={drawerWidth}
          loading={retro.loading} error={retro.error} data={retro.data} stale={stale}
          selectedCandidateIndex={selectedCandidateIndex} onSelectedCandidateIndexChange={setSelectedCandidateIndex}
          onWidthChange={setDrawerWidth} onClose={() => { setAdjusting(false); setDrawerOpen(false); }}
          onOpen={() => { setAdjusting(false); setDrawerOpen(true); }}
          restoreFocusTarget={adjusting ? targetRef.current : null}
          onAdjustParameters={() => { setAdjusting(true); setDrawerOpen(false); }} />
      </div>
    </div>
  </div>;
}
