import { expect } from 'vitest';
import { useState, type ReactNode } from 'react';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { StructureLibraryProvider, useStructureLibrary } from '../structure-library/StructureLibraryProvider';
import type { StructureWorkspace } from '../structure/workspace';
import type { StructureDocument, ImportedStructure } from '../structure-library/types';
export function testDocument(smiles: string): StructureDocument {
  return { smiles, ket: JSON.stringify({ root: { nodes: [{ $ref: 'mol0' }] }, mol0: { atoms: [{ label: 'C' }], bonds: [] } }),
    snapshot_schema_version: 1, editor_version: '3.8.0' };
}
export function testSelection(smiles: string): ImportedStructure {
  return { smiles, document: testDocument(smiles), source: { kind: 'saved', id: 'saved-a', name: '已选候选 A' } };
}
function Seed({ initial, children }: { initial: Record<string, unknown>; children: ReactNode }) {
  const library = useStructureLibrary();
  useState(() => { for (const [key, value] of Object.entries(initial)) library?.drafts.set(key, value); });
  return children;
}
export function LibraryTestProvider({ workspace, initial = {}, children }: { workspace: StructureWorkspace; initial?: Record<string, unknown>; children: ReactNode }) {
  return <StructureLibraryProvider workspace={workspace}><Seed initial={initial}>{children}</Seed></StructureLibraryProvider>;
}
export async function chooseCurrentStructure(button?: HTMLElement) {
  if (button) { await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(button); }
  const dialog = await screen.findByRole('dialog', { name: /选择结构/ });
  fireEvent.click(within(dialog).getByRole('button', { name: '当前画板' }));
  fireEvent.click(within(dialog).getByLabelText('使用当前画板'));
  fireEvent.click(within(dialog).getByRole('button', { name: '导入' }));
  return dialog;
}
