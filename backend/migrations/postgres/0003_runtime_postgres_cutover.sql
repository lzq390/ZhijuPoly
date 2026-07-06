CREATE SEQUENCE IF NOT EXISTS online_knowledge.history_history_id_seq AS bigint;

SELECT setval(
  'online_knowledge.history_history_id_seq',
  COALESCE((SELECT max(history_id) FROM online_knowledge.history), 1),
  EXISTS (SELECT 1 FROM online_knowledge.history)
);

ALTER TABLE online_knowledge.history
  ALTER COLUMN history_id SET DEFAULT nextval('online_knowledge.history_history_id_seq');

ALTER SEQUENCE online_knowledge.history_history_id_seq
  OWNED BY online_knowledge.history.history_id;

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_title_zh_trgm
ON knowledge.documents USING gin (title_zh gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_title_en_trgm
ON knowledge.documents USING gin (title_en gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_abstract_trgm
ON knowledge.documents USING gin (abstract gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_claim_trgm
ON knowledge.documents USING gin (claim gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_polymer_iupac_trgm
ON knowledge.documents USING gin (polymer_iupac gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_formulation_trgm
ON knowledge.documents USING gin (formulation gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_pi_monomer_iupac_name
ON pi.monomer_iupac(iupac_name);

UPDATE governance.source_files
SET status = 'archived_legacy_runtime_source',
    notes = CASE
      WHEN notes IS NULL OR notes = '' THEN 'Retained for audit/import rollback; not required by default runtime.'
      ELSE notes || ' Retained for audit/import rollback; not required by default runtime.'
    END,
    updated_at = now()
WHERE logical_name IN ('main_sqlite', 'pi_sqlite', 'dft_sqlite');
