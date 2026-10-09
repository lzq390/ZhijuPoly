export type StructureDocument = {
  smiles: string;
  ket: string;
  snapshot_schema_version: 1;
  editor_version: string;
  revision?: number;
};
export type StructureRecordSummary = Omit<StructureDocument, 'ket' | 'revision'> & {
  id: string; name: string; canonical_smiles: string; preview_svg: string | null;
  metadata_revision: number; created_at: string; updated_at: string;
};
export type StructureRecord = StructureRecordSummary & { ket: Record<string, unknown> };
export type ImportedStructure = {
  source: { kind: 'saved'; id: string; name: string } | { kind: 'canvas' };
  smiles: string;
  document: StructureDocument;
};
export type StructureLibraryStatus = {
  available: boolean; enabled: boolean; schema_ready: boolean; message: string;
  max_records: number; max_bytes: number; max_ket_bytes: number;
};
export const recordDocument = (record: StructureRecord): StructureDocument => ({
  smiles: record.smiles, ket: JSON.stringify(record.ket),
  snapshot_schema_version: record.snapshot_schema_version, editor_version: record.editor_version
});
export const sourceLabel = (value: ImportedStructure) => value.source.kind === 'saved' ? value.source.name : '当前画板';
