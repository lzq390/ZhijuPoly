CREATE SCHEMA structure_library;
REVOKE ALL ON SCHEMA structure_library FROM PUBLIC;

CREATE TABLE structure_library.records (
    id uuid PRIMARY KEY,
    owner_user_id uuid NOT NULL REFERENCES auth.users(user_id),
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 100),
    smiles text NOT NULL CHECK (char_length(smiles) BETWEEN 1 AND 8000),
    canonical_smiles text NOT NULL,
    ket jsonb NOT NULL CHECK (jsonb_typeof(ket) = 'object'),
    snapshot_schema_version integer NOT NULL CHECK (snapshot_schema_version = 1),
    editor_version text NOT NULL,
    preview_svg text,
    metadata_revision integer NOT NULL DEFAULT 1 CHECK (metadata_revision > 0),
    client_request_id uuid NOT NULL,
    create_payload_hash text NOT NULL,
    storage_bytes bigint NOT NULL CHECK (storage_bytes > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, client_request_id)
);
CREATE INDEX structure_records_owner_created ON structure_library.records
    (owner_user_id, created_at DESC, id DESC);
ALTER TABLE structure_library.records ENABLE ROW LEVEL SECURITY;
ALTER TABLE structure_library.records FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON structure_library.records TO nexpoly_api
    USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY audit_access ON structure_library.records FOR SELECT TO nexpoly_mutable_audit USING (true);
GRANT USAGE ON SCHEMA structure_library TO nexpoly_api, nexpoly_mutable_audit;
GRANT SELECT, INSERT, DELETE ON structure_library.records TO nexpoly_api;
GRANT UPDATE (name, metadata_revision, updated_at) ON structure_library.records TO nexpoly_api;
GRANT SELECT ON structure_library.records TO nexpoly_mutable_audit;
