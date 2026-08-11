CREATE TABLE IF NOT EXISTS governance.property_filter_options_snapshots (
  snapshot_key text PRIMARY KEY,
  schema_version integer NOT NULL,
  generation bigint NOT NULL CHECK (generation > 0),
  import_batch_id bigint,
  source_sha256 text,
  generated_at timestamptz NOT NULL,
  total_records bigint NOT NULL CHECK (total_records >= 0),
  mapped_records bigint NOT NULL CHECK (mapped_records >= 0),
  raw_records bigint NOT NULL CHECK (raw_records >= 0),
  options jsonb NOT NULL CHECK (jsonb_typeof(options) = 'array'),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (snapshot_key = 'current'),
  CHECK (mapped_records + raw_records = total_records)
);

CREATE INDEX IF NOT EXISTS idx_core_filter_records_standardized_unit_value
  ON core.polymer_property_filter_records (
    property_key,
    (COALESCE(canonical_unit, '')),
    canonical_value
  )
  WHERE property_key IS NOT NULL AND canonical_value IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_core_filter_records_raw_unit_value_v2
  ON core.polymer_property_filter_records (
    property_name,
    (COALESCE(property_unit_clean, '')),
    property_value_num
  )
  WHERE property_key IS NULL AND property_value_num IS NOT NULL;

CREATE STATISTICS IF NOT EXISTS stats_core_filter_records_standardized_unit
  (dependencies, mcv)
  ON property_key, canonical_unit
  FROM core.polymer_property_filter_records;

CREATE STATISTICS IF NOT EXISTS stats_core_filter_records_raw_unit
  (dependencies, mcv)
  ON property_name, property_unit_clean
  FROM core.polymer_property_filter_records;

ANALYZE core.polymer_property_filter_records;

WITH standardized AS (
  SELECT
    'standardized'::text AS filter_type,
    'std:' || property_key || ':' || COALESCE(canonical_unit, '') AS option_key,
    COALESCE(NULLIF(MIN(NULLIF(property_label, '')), ''), property_key) AS label,
    property_key,
    NULL::text AS property_name,
    NULL::text AS property_unit_clean,
    canonical_unit,
    COUNT(*) AS rows,
    COUNT(DISTINCT COALESCE(
      NULLIF(canonical_smiles, ''),
      NULLIF(smiles, ''),
      'record:' || filter_record_id::text
    )) AS unique_smiles,
    MIN(canonical_value) AS min_value,
    percentile_cont(0.05) WITHIN GROUP (ORDER BY canonical_value) AS p5_value,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY canonical_value) AS median_value,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY canonical_value) AS p95_value,
    MAX(canonical_value) AS max_value
  FROM core.polymer_property_filter_records
  WHERE property_key IS NOT NULL AND canonical_value IS NOT NULL
  GROUP BY property_key, canonical_unit
),
raw AS (
  SELECT
    'raw'::text AS filter_type,
    'raw:' || md5(property_name || '|' || COALESCE(property_unit_clean, '')) AS option_key,
    CASE
      WHEN COALESCE(NULLIF(property_unit_clean, ''), '') = '' THEN property_name
      ELSE property_name || ' (' || property_unit_clean || ')'
    END AS label,
    NULL::text AS property_key,
    property_name,
    property_unit_clean,
    NULL::text AS canonical_unit,
    COUNT(*) AS rows,
    COUNT(DISTINCT COALESCE(
      NULLIF(canonical_smiles, ''),
      NULLIF(smiles, ''),
      'record:' || filter_record_id::text
    )) AS unique_smiles,
    MIN(property_value_num) AS min_value,
    percentile_cont(0.05) WITHIN GROUP (ORDER BY property_value_num) AS p5_value,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY property_value_num) AS median_value,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY property_value_num) AS p95_value,
    MAX(property_value_num) AS max_value
  FROM core.polymer_property_filter_records
  WHERE property_key IS NULL AND property_value_num IS NOT NULL
  GROUP BY property_name, property_unit_clean
),
ordered_options AS (
  SELECT * FROM standardized
  UNION ALL
  SELECT * FROM raw
),
catalog AS (
  SELECT COALESCE(
    jsonb_agg(
      jsonb_build_object(
        'filter_type', filter_type,
        'option_key', option_key,
        'label', label,
        'property_key', property_key,
        'property_name', property_name,
        'property_unit_clean', property_unit_clean,
        'canonical_unit', canonical_unit,
        'rows', rows,
        'unique_smiles', unique_smiles,
        'min_value', min_value,
        'p5_value', p5_value,
        'median_value', median_value,
        'p95_value', p95_value,
        'max_value', max_value
      )
      ORDER BY
        CASE filter_type WHEN 'standardized' THEN 0 ELSE 1 END,
        rows DESC,
        label ASC
    ),
    '[]'::jsonb
  ) AS options
  FROM ordered_options
),
summary AS (
  SELECT
    COUNT(*) AS total_records,
    COUNT(*) FILTER (WHERE property_key IS NOT NULL) AS mapped_records,
    COUNT(*) FILTER (WHERE property_key IS NULL) AS raw_records
  FROM core.polymer_property_filter_records
),
latest_batch AS (
  SELECT
    batches.import_batch_id,
    batches.finished_at,
    sources.sha256
  FROM governance.import_batches batches
  LEFT JOIN governance.source_files sources
    ON sources.source_file_id = batches.source_file_id
  WHERE batches.dataset_key = 'property_filter'
    AND batches.status IN ('completed', 'empty')
  ORDER BY batches.import_batch_id DESC
  LIMIT 1
)
INSERT INTO governance.property_filter_options_snapshots (
  snapshot_key, schema_version, generation, import_batch_id,
  source_sha256, generated_at, total_records, mapped_records,
  raw_records, options, updated_at
)
SELECT
  'current',
  1,
  1,
  latest_batch.import_batch_id,
  latest_batch.sha256,
  COALESCE(latest_batch.finished_at, now()),
  summary.total_records,
  summary.mapped_records,
  summary.raw_records,
  catalog.options,
  now()
FROM summary
CROSS JOIN catalog
LEFT JOIN latest_batch ON true
ON CONFLICT (snapshot_key) DO NOTHING;
