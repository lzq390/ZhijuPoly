CREATE SCHEMA IF NOT EXISTS governance;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS knowledge;
CREATE SCHEMA IF NOT EXISTS online_knowledge;
CREATE SCHEMA IF NOT EXISTS pi;
CREATE SCHEMA IF NOT EXISTS dft;
CREATE SCHEMA IF NOT EXISTS lab;
CREATE SCHEMA IF NOT EXISTS experimental;
CREATE SCHEMA IF NOT EXISTS model_registry;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS governance.schema_migrations (
  version text PRIMARY KEY,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS governance.source_files (
  source_file_id bigserial PRIMARY KEY,
  logical_name text NOT NULL UNIQUE,
  path text NOT NULL,
  storage_kind text NOT NULL DEFAULT 'file',
  status text NOT NULL DEFAULT 'unknown',
  row_count bigint,
  byte_size bigint,
  sha256 text,
  notes text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS governance.import_batches (
  import_batch_id bigserial PRIMARY KEY,
  dataset_key text NOT NULL,
  source_file_id bigint REFERENCES governance.source_files(source_file_id),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'running',
  row_count bigint NOT NULL DEFAULT 0,
  error_message text
);

CREATE TABLE IF NOT EXISTS core.polymers (
  polymer_id bigint PRIMARY KEY,
  polymer_name text,
  smiles text NOT NULL UNIQUE,
  canonical_smiles text,
  rdkit_parse_ok boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.polymer_properties (
  property_id bigint PRIMARY KEY,
  polymer_id bigint NOT NULL REFERENCES core.polymers(polymer_id) ON DELETE CASCADE,
  property_category text NOT NULL DEFAULT 'Others',
  property_name text NOT NULL,
  property_value text NOT NULL,
  property_value_num double precision,
  property_unit text,
  label_source text,
  source_row_number bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_core_polymers_smiles ON core.polymers(smiles);
CREATE INDEX IF NOT EXISTS idx_core_polymers_canonical_smiles ON core.polymers(canonical_smiles);
CREATE INDEX IF NOT EXISTS idx_core_polymers_parse_ok ON core.polymers(rdkit_parse_ok);
CREATE INDEX IF NOT EXISTS idx_core_properties_polymer_id ON core.polymer_properties(polymer_id);
CREATE INDEX IF NOT EXISTS idx_core_properties_category ON core.polymer_properties(property_category);
CREATE INDEX IF NOT EXISTS idx_core_properties_name_trgm ON core.polymer_properties USING gin (property_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS knowledge.documents (
  knowledge_id bigint PRIMARY KEY,
  source_file text NOT NULL,
  source_row_number bigint NOT NULL,
  source_sequence text,
  title_zh text,
  title_en text,
  abstract text NOT NULL,
  claim text,
  analysis text,
  is_polymer_synthesis text,
  judgement_reason text,
  polymer_iupac text,
  formulation text,
  catalyst text,
  temperature text,
  reaction_time text,
  solvent text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_file, source_row_number)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_source_file ON knowledge.documents(source_file);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_title_en ON knowledge.documents(title_en);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_polymer_iupac ON knowledge.documents(polymer_iupac);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_formulation_trgm ON knowledge.documents USING gin (formulation gin_trgm_ops);

CREATE TABLE IF NOT EXISTS knowledge.formulation_records (
  formulation_id bigserial PRIMARY KEY,
  knowledge_id bigint NOT NULL UNIQUE REFERENCES knowledge.documents(knowledge_id) ON DELETE CASCADE,
  source_file text NOT NULL,
  source_row_number bigint NOT NULL,
  polymer_iupac text,
  formulation text,
  catalyst text,
  temperature text,
  reaction_time text,
  solvent text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_formulation_records_formulation_trgm ON knowledge.formulation_records USING gin (formulation gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_formulation_records_polymer_iupac ON knowledge.formulation_records(polymer_iupac);

CREATE TABLE IF NOT EXISTS online_knowledge.history (
  history_id bigint PRIMARY KEY,
  material text NOT NULL,
  mode text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  papers_found integer NOT NULL DEFAULT 0,
  reactions_extracted integer NOT NULL DEFAULT 0,
  max_papers integer NOT NULL DEFAULT 0,
  result_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(material, mode)
);

CREATE INDEX IF NOT EXISTS idx_online_history_created_at ON online_knowledge.history(created_at);
CREATE INDEX IF NOT EXISTS idx_online_history_result_data ON online_knowledge.history USING gin (result_data);

CREATE TABLE IF NOT EXISTS online_knowledge.jobs (
  job_id text PRIMARY KEY,
  status text NOT NULL,
  material text NOT NULL,
  mode text NOT NULL,
  max_papers integer NOT NULL,
  progress_stage text NOT NULL DEFAULT 'pending',
  progress_message text NOT NULL DEFAULT 'Waiting for the search worker to start.',
  processed_papers integer NOT NULL DEFAULT 0,
  total_papers integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  error_message text,
  result_data jsonb
);

CREATE INDEX IF NOT EXISTS idx_online_jobs_created_at ON online_knowledge.jobs(created_at);

CREATE TABLE IF NOT EXISTS pi.polymers (
  id bigint PRIMARY KEY,
  mon1 text NOT NULL,
  mon2 text NOT NULL,
  polym text NOT NULL,
  canonical_polym text,
  smiles_valid boolean NOT NULL DEFAULT false,
  morgan_fp bytea,
  created_at timestamptz
);

CREATE TABLE IF NOT EXISTS pi.tg_predictions (
  id bigint PRIMARY KEY REFERENCES pi.polymers(id) ON DELETE CASCADE,
  tg_celsius double precision NOT NULL,
  smiles_valid boolean NOT NULL DEFAULT false,
  dielectric_const_dc double precision,
  static_dielectric_const double precision,
  dipole_debye double precision,
  electrophilicity_index double precision,
  homo_lumo_gap_ev double precision,
  hardness double precision,
  mulliken_electronegativity double precision,
  redox_window_v double precision,
  linear_expansion double precision,
  refractive_index double precision,
  created_at timestamptz
);

CREATE TABLE IF NOT EXISTS pi.monomer_iupac (
  smiles text PRIMARY KEY,
  iupac_name text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pi_tg_predictions_tg_id ON pi.tg_predictions(tg_celsius, id);
CREATE INDEX IF NOT EXISTS idx_pi_tg_predictions_valid ON pi.tg_predictions(smiles_valid);
CREATE INDEX IF NOT EXISTS idx_pi_polymers_morgan_fp ON pi.polymers(id) WHERE morgan_fp IS NOT NULL;

CREATE TABLE IF NOT EXISTS dft.molecule_final (
  mol_id text PRIMARY KEY,
  range_group text NOT NULL,
  final_step integer NOT NULL,
  n_atoms integer NOT NULL,
  coordinates text NOT NULL,
  scf_energy double precision,
  zero_point_energy double precision,
  thermal_enthalpy double precision,
  gibbs_free_energy double precision,
  lowest_freq double precision,
  dipole_moment double precision,
  homo_ev double precision,
  lumo_ev double precision,
  gap_ev double precision,
  is_converged text,
  pca_x double precision NOT NULL,
  pca_y double precision NOT NULL,
  pca_z double precision NOT NULL
);

CREATE TABLE IF NOT EXISTS dft.energy_trace (
  mol_id text NOT NULL REFERENCES dft.molecule_final(mol_id) ON DELETE CASCADE,
  step integer NOT NULL,
  scf_energy double precision,
  homo_ev double precision,
  lumo_ev double precision,
  gap_ev double precision,
  PRIMARY KEY (mol_id, step)
);

CREATE INDEX IF NOT EXISTS idx_dft_final_pca ON dft.molecule_final(pca_x, pca_y, pca_z);
CREATE INDEX IF NOT EXISTS idx_dft_trace_mol_step ON dft.energy_trace(mol_id, step);

CREATE TABLE IF NOT EXISTS lab.test_projects (
  id integer PRIMARY KEY,
  project_name varchar(100) NOT NULL UNIQUE,
  result_unit varchar(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS lab.sample_measurements (
  id integer PRIMARY KEY,
  sample_id varchar(50) NOT NULL UNIQUE,
  experiment_project varchar(100) NOT NULL,
  instrument_id varchar(50) NOT NULL,
  "operator" varchar(100) NOT NULL,
  collection_time timestamp NOT NULL,
  temperature numeric(5, 2),
  concentration numeric(10, 4),
  result_value numeric(10, 4) NOT NULL,
  result_unit varchar(20) NOT NULL,
  remarks text
);

CREATE TABLE IF NOT EXISTS experimental.process_records (
  record_id bigserial PRIMARY KEY,
  source_file text NOT NULL,
  source_row_number bigint NOT NULL,
  polymer_id text,
  polymer_name text,
  product_name text,
  process_flow_original_text text,
  material_original_text text,
  raw_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(source_file, source_row_number)
);

CREATE TABLE IF NOT EXISTS experimental.property_records (
  record_id bigserial PRIMARY KEY,
  source_file text NOT NULL,
  source_row_number bigint NOT NULL,
  polymer_id text,
  polymer_name text,
  property_category text,
  property_name_en text,
  value text,
  raw_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(source_file, source_row_number)
);

CREATE TABLE IF NOT EXISTS model_registry.assets (
  asset_id bigserial PRIMARY KEY,
  logical_name text NOT NULL UNIQUE,
  path text NOT NULL,
  asset_type text NOT NULL,
  byte_size bigint,
  sha256 text,
  status text NOT NULL DEFAULT 'unknown',
  notes text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP VIEW IF EXISTS public.pi_tg_predictions;
DROP VIEW IF EXISTS public.pi_polymers;
DROP VIEW IF EXISTS public.pi_monomer_iupac;

CREATE VIEW public.pi_polymers AS
SELECT id, mon1, mon2, polym, canonical_polym, smiles_valid, morgan_fp, created_at
FROM pi.polymers;

CREATE VIEW public.pi_tg_predictions AS
SELECT
  id,
  tg_celsius,
  smiles_valid,
  dielectric_const_dc,
  static_dielectric_const,
  dipole_debye,
  electrophilicity_index,
  homo_lumo_gap_ev,
  hardness,
  mulliken_electronegativity,
  redox_window_v,
  linear_expansion,
  refractive_index,
  created_at
FROM pi.tg_predictions;

CREATE VIEW public.pi_monomer_iupac AS
SELECT smiles, iupac_name, created_at
FROM pi.monomer_iupac;
