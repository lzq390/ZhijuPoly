// @vitest-environment jsdom
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StructureWorkspace } from '../structure/workspace';
import { retireSession } from '../auth/session';
import { testDocument } from '../test/structureLibraryFixture';
import { MonomerPairEditor } from '../components/monomer-polymerization/MonomerPairEditor';
import { SaveStructureDialog, StructureImportButton, StructureLibraryProvider, useModuleDraftState } from './StructureLibraryProvider';
import type { StructureRecord } from './types';
const api = vi.hoisted(() => ({ status: vi.fn(), list: vi.fn(), detail: vi.fn(), save: vi.fn(), rename: vi.fn(), remove: vi.fn() }));
vi.mock('./api', () => ({ structureLibraryApi: api }));
vi.mock('../services/api', () => ({ fetchStructure2D: vi.fn(async () => ({ structure_svg: '<svg xmlns="http://www.w3.org/2000/svg"/>' })) }));
function record(id: string, smiles: string): StructureRecord {
  return { ...testDocument(smiles), ket: JSON.parse(testDocument(smiles).ket), id, name: `记录 ${id}`,
    canonical_smiles: smiles, preview_svg: null, metadata_revision: 1, created_at: '2026-10-08T00:00:00Z', updated_at: '2026-10-08T00:00:00Z' };
}
const records = [record('A', 'CCO'), record('B', 'CCN')];
function Pair({ submit }: { submit: (value: string[]) => void }) {
  const [a, setA] = useState(''), [b, setB] = useState('');
  return <><MonomerPairEditor monomerA={a} monomerB={b} monomerBRequired monomerBRequirementNote="必填"
    monomerAError={null} monomerBError={null} onMonomerAChange={setA} onMonomerBChange={setB} onTouched={() => {}} onEditStructure={() => {}} />
    <button onClick={() => submit([a, b])}>提交测试表单</button></>;
}
function mountPair() {
  const workspace = new StructureWorkspace('CCC');
  const capture = vi.spyOn(workspace, 'captureForImport');
  const submit = vi.fn();
  const view = render(<StructureLibraryProvider workspace={workspace}><Pair submit={submit} /></StructureLibraryProvider>);
  return { workspace, capture, submit, ...view };
}
async function select(id: string, trigger: HTMLElement) {
  fireEvent.click(trigger);
  const option = await screen.findByRole('radio', { name: new RegExp('记录 ' + id) });
  fireEvent.click(option);
  fireEvent.click(screen.getByRole('button', { name: '导入' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
}
beforeEach(() => {
  vi.resetAllMocks();
  api.status.mockResolvedValue({ available: true });
  api.list.mockResolvedValue({ items: records, next_cursor: null });
  api.detail.mockImplementation(async id => records.find(record => record.id === id));
  api.save.mockResolvedValue(records[0]);
});
afterEach(cleanup);

describe('personal structure library', () => {
  it('imports A and B independently while the canvas remains C, with no implicit current read', async () => {
    const { workspace, capture, submit } = mountPair();
    const triggers = screen.getAllByRole('button', { name: '选择结构' });
    await select('A', triggers[0]);
    await select('B', triggers[1]);
    fireEvent.click(screen.getByRole('button', { name: '提交测试表单' }));
    expect(submit).toHaveBeenCalledWith(['CCO', 'CCN']);
    expect(workspace.getSnapshot().smiles).toBe('CCC');
    expect(capture).not.toHaveBeenCalled();
    expect(screen.getByText('来自：记录 A')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('SMILES', { selector: '#monomer-a-smiles' }), { target: { value: 'NN' } });
    expect(screen.queryByText('来自：记录 A')).toBeNull();
  });

  it('does not preselect a record or change any input on cancel', async () => {
    const { submit } = mountPair();
    fireEvent.click(screen.getAllByRole('button', { name: '选择结构' })[0]);
    await screen.findByText('记录 A');
    expect(screen.getByRole('button', { name: '导入' })).toHaveProperty('disabled', true);
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: '提交测试表单' }));
    expect(submit).toHaveBeenCalledWith(['', '']);
    expect(api.detail).not.toHaveBeenCalled();
  });

  it('can select a saved structure despite an invalid canvas draft and handles deleted records', async () => {
    const { workspace, capture } = mountPair();
    workspace.setDraft('C(', 'invalid');
    api.detail.mockRejectedValueOnce(new Error('结构记录不存在或已删除。'));
    fireEvent.click(screen.getAllByRole('button', { name: '选择结构' })[0]);
    fireEvent.click(await screen.findByRole('radio', { name: /记录 A/ }));
    fireEvent.click(screen.getByRole('button', { name: '导入' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('SMILES', { selector: '#monomer-a-smiles' })).toHaveProperty('value', '');
    fireEvent.click(screen.getByRole('button', { name: '导入' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByLabelText('SMILES', { selector: '#monomer-a-smiles' })).toHaveProperty('value', 'CCO');
    expect(capture).not.toHaveBeenCalled();
  });

  it('ignores detail responses after target edits or account retirement', async () => {
    const { submit } = mountPair();
    let resolve!: (record: StructureRecord) => void;
    api.detail.mockImplementation(() => new Promise(done => { resolve = done; }));
    fireEvent.click(screen.getAllByRole('button', { name: '选择结构' })[0]);
    fireEvent.click(await screen.findByRole('radio', { name: /记录 A/ }));
    fireEvent.click(screen.getByRole('button', { name: '导入' }));
    fireEvent.change(screen.getByLabelText('SMILES', { selector: '#monomer-a-smiles' }), { target: { value: 'NN' } });
    await act(async () => resolve(records[0]));
    fireEvent.click(screen.getByRole('button', { name: '提交测试表单' }));
    expect(submit).toHaveBeenLastCalledWith(['NN', '']);
    fireEvent.click(screen.getAllByRole('button', { name: '选择结构' })[1]);
    fireEvent.click(await screen.findByRole('radio', { name: /记录 B/ }));
    fireEvent.click(screen.getByRole('button', { name: '导入' }));
    await act(async () => { retireSession(); resolve(records[1]); });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByLabelText('SMILES', { selector: '#monomer-b-smiles' })).toHaveProperty('value', '');
  });

  it('saves a fixed snapshot and retries with the same idempotency key', async () => {
    const workspace = new StructureWorkspace('CCC');
    const document = testDocument('CCO');
    const close = vi.fn();
    api.save.mockRejectedValueOnce(new Error('网络中断'));
    render(<SaveStructureDialog document={document} workspace={workspace} onClose={close} />);
    fireEvent.change(screen.getByLabelText('结构名称'), { target: { value: '候选 A' } });
    fireEvent.click(screen.getByRole('button', { name: '确认保存' }));
    await screen.findByText('网络中断');
    workspace.commitSmiles('NN');
    fireEvent.click(screen.getByRole('button', { name: '确认保存' }));
    await waitFor(() => expect(close).toHaveBeenCalledWith(true));
    expect(api.save.mock.calls[0][0].smiles).toBe('CCO');
    expect(api.save.mock.calls[1].slice(0, 3)).toEqual(api.save.mock.calls[0].slice(0, 3));
    expect(workspace.hasUnrecordedChanges()).toBe(true);
  });

  it('retains module drafts across navigation and clears them on account retirement', () => {
    function Draft() { const [value, set] = useModuleDraftState('dft.smiles', ''); return <input aria-label="独立草稿" value={value} onChange={e => set(e.target.value)} />; }
    const workspace = new StructureWorkspace('CCC');
    const tree = (open: boolean) => <StructureLibraryProvider workspace={workspace}>{open && <Draft />}</StructureLibraryProvider>;
    const view = render(tree(true));
    fireEvent.change(screen.getByLabelText('独立草稿'), { target: { value: 'N(' } });
    view.rerender(tree(false)); view.rerender(tree(true));
    expect(screen.getByLabelText('独立草稿')).toHaveProperty('value', 'N(');
    act(() => retireSession());
    view.rerender(tree(false)); view.rerender(tree(true));
    expect(screen.getByLabelText('独立草稿')).toHaveProperty('value', '');
    expect(workspace.getSnapshot().smiles).toBe('CCC');
  });
});
