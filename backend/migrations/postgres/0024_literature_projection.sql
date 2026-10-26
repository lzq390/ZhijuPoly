-- Additive, independently published literature projection. Source DWD is read-only.
CREATE TABLE knowledge.literature_generations (
  generation_id uuid PRIMARY KEY,
  status text NOT NULL CHECK (status IN ('building', 'ready', 'published', 'failed')),
  is_active boolean NOT NULL DEFAULT false,
  normalizer_version text NOT NULL,
  source_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  report jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  CHECK (NOT is_active OR status = 'published')
);
CREATE UNIQUE INDEX literature_one_active_generation
  ON knowledge.literature_generations (is_active) WHERE is_active;

CREATE TABLE knowledge.literature_snapshots (
  snapshot_uid text PRIMARY KEY,
  document_uid text NOT NULL,
  source_paper_id text NOT NULL,
  source_extract_id text NOT NULL,
  source_extract_created_at timestamptz,
  model_version text,
  normalizer_version text NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  raw_json jsonb NOT NULL CHECK (jsonb_typeof(raw_json) = 'object'),
  normalized_json jsonb NOT NULL CHECK (jsonb_typeof(normalized_json) = 'object'),
  assets jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(assets) = 'array'),
  quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(quality_flags) = 'array'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_uid, snapshot_uid)
);

CREATE TABLE knowledge.literature_catalog (
  generation_id uuid NOT NULL REFERENCES knowledge.literature_generations(generation_id),
  document_uid text NOT NULL,
  doi text NOT NULL,
  snapshot_uid text NOT NULL,
  title text NOT NULL,
  authors jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(authors) = 'array'),
  journal text,
  publication_year integer,
  keywords jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(keywords) = 'array'),
  keywords_text text NOT NULL DEFAULT '',
  abstract text NOT NULL DEFAULT '',
  source_paper_ids jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(source_paper_ids) = 'array'),
  capabilities jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(capabilities) = 'object'),
  counts jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counts) = 'object'),
  PRIMARY KEY (generation_id, document_uid),
  UNIQUE (generation_id, doi),
  FOREIGN KEY (document_uid, snapshot_uid)
    REFERENCES knowledge.literature_snapshots(document_uid, snapshot_uid)
);
CREATE INDEX literature_catalog_title_trgm ON knowledge.literature_catalog USING gin (title gin_trgm_ops);
CREATE INDEX literature_catalog_abstract_trgm ON knowledge.literature_catalog USING gin (abstract gin_trgm_ops);
CREATE INDEX literature_catalog_keywords_trgm ON knowledge.literature_catalog USING gin (keywords_text gin_trgm_ops);
CREATE INDEX literature_catalog_year ON knowledge.literature_catalog(generation_id, publication_year DESC, document_uid);

-- Snapshots are append-only, including for maintenance users. A refreshed source
-- record or normalizer version gets a new content-addressed snapshot UID.
CREATE FUNCTION knowledge.reject_literature_snapshot_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'Literature snapshots are immutable';
END
$$;
CREATE TRIGGER literature_snapshot_immutable BEFORE UPDATE OR DELETE
  ON knowledge.literature_snapshots FOR EACH ROW
  EXECUTE FUNCTION knowledge.reject_literature_snapshot_mutation();

CREATE FUNCTION knowledge.protect_published_literature_catalog() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE target_generations uuid[];
BEGIN
  IF TG_OP = 'INSERT' THEN
    target_generations := ARRAY[NEW.generation_id];
  ELSIF TG_OP = 'DELETE' THEN
    target_generations := ARRAY[OLD.generation_id];
  ELSE
    target_generations := ARRAY[OLD.generation_id, NEW.generation_id];
  END IF;
  -- Lock by identity before checking status. A concurrent publisher may still
  -- expose its old ready row to a plain SELECT while holding FOR UPDATE; the
  -- catalog's later foreign-key check alone cannot protect published contents.
  -- A stable order also covers updates moving a row between generations.
  PERFORM generation_id FROM knowledge.literature_generations
    WHERE generation_id = ANY(target_generations)
    ORDER BY generation_id FOR SHARE;
  IF EXISTS (SELECT 1 FROM knowledge.literature_generations
             WHERE generation_id = ANY(target_generations) AND status = 'published') THEN
    RAISE EXCEPTION 'Published literature catalogs are immutable';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER literature_catalog_published BEFORE INSERT OR UPDATE OR DELETE
  ON knowledge.literature_catalog FOR EACH ROW
  EXECUTE FUNCTION knowledge.protect_published_literature_catalog();

CREATE FUNCTION knowledge.protect_published_literature_generation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF OLD.status = 'published' AND
     (NEW.status <> 'published' OR
      (to_jsonb(NEW) - 'is_active') IS DISTINCT FROM (to_jsonb(OLD) - 'is_active')) THEN
    RAISE EXCEPTION 'Published literature generation metadata is immutable';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER literature_generation_published BEFORE UPDATE
  ON knowledge.literature_generations FOR EACH ROW
  EXECUTE FUNCTION knowledge.protect_published_literature_generation();

-- Provision a distinct LOGIN inheriting this maintenance group outside migrations.
DO $$ BEGIN
  -- Attribute checks alone cannot attest every inherited or existing ACL.
  -- A pre-existing role requires a separate operator review, never adoption.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexpoly_literature_import') THEN
    RAISE EXCEPTION 'Literature maintenance group already exists; explicit role provenance review required';
  END IF;
  CREATE ROLE nexpoly_literature_import NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
END $$;
GRANT USAGE ON SCHEMA knowledge TO nexpoly_literature_import;
GRANT SELECT, INSERT, UPDATE ON knowledge.literature_generations TO nexpoly_literature_import;
GRANT SELECT, INSERT ON knowledge.literature_catalog, knowledge.literature_snapshots TO nexpoly_literature_import;
DO $$ DECLARE runtime_role text; BEGIN
  FOREACH runtime_role IN ARRAY ARRAY['nexpoly_api', 'nexpoly_service', 'nexpoly_mutable_audit'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = runtime_role) THEN
      EXECUTE format('GRANT USAGE ON SCHEMA knowledge TO %I', runtime_role);
      EXECUTE format('GRANT SELECT ON knowledge.literature_generations, knowledge.literature_catalog, knowledge.literature_snapshots TO %I', runtime_role);
    END IF;
  END LOOP;
END $$;
