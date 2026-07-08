CREATE TABLE IF NOT EXISTS core.polymer_property_filter_records (
  filter_record_id bigint PRIMARY KEY,
  source_file text NOT NULL,
  source_row_number bigint NOT NULL,
  polymer_name text,
  smiles text,
  canonical_smiles text,
  rdkit_parse_ok boolean NOT NULL DEFAULT false,
  property_category text NOT NULL DEFAULT 'Others',
  property_name text NOT NULL,
  property_value text NOT NULL,
  property_value_num double precision,
  property_unit text,
  property_unit_raw text,
  property_unit_clean text,
  property_key text,
  property_label text,
  canonical_value double precision,
  canonical_unit text,
  unit_conversion_status text,
  value_origin text,
  label_source text,
  reliable_score double precision,
  soft_quality_flags text,
  duplicate_flag text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_core_filter_records_property_key_value
  ON core.polymer_property_filter_records(property_key, canonical_value)
  WHERE property_key IS NOT NULL AND canonical_value IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_core_filter_records_raw_property_value
  ON core.polymer_property_filter_records(property_name, property_unit_clean, property_value_num)
  WHERE property_value_num IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_core_filter_records_smiles
  ON core.polymer_property_filter_records(smiles);
CREATE INDEX IF NOT EXISTS idx_core_filter_records_canonical_smiles
  ON core.polymer_property_filter_records(canonical_smiles);
CREATE INDEX IF NOT EXISTS idx_core_filter_records_value_origin
  ON core.polymer_property_filter_records(value_origin);
CREATE INDEX IF NOT EXISTS idx_core_filter_records_reliable_score
  ON core.polymer_property_filter_records(reliable_score);
CREATE INDEX IF NOT EXISTS idx_core_filter_records_soft_flags_trgm
  ON core.polymer_property_filter_records USING gin (soft_quality_flags gin_trgm_ops);
