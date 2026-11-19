-- Private documents for the dedicated dev-m1 cluster. No production schema changes.
BEGIN;
CREATE SCHEMA openscience_m4_knowledge;
REVOKE ALL ON SCHEMA openscience_m4_knowledge FROM PUBLIC;
GRANT USAGE ON SCHEMA openscience_m4_knowledge TO nexpoly_api;

CREATE TABLE openscience_m4_knowledge.collections (
    kb_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES auth.users(user_id),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name),
    UNIQUE (owner_id, kb_id)
);
CREATE TABLE openscience_m4_knowledge.documents (
    doc_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL,
    kb_id uuid NOT NULL,
    filename text NOT NULL CHECK (length(filename) BETWEEN 1 AND 240),
    mime_type text NOT NULL,
    original bytea NOT NULL CHECK (octet_length(original) BETWEEN 1 AND 262144),
    chunk_count integer NOT NULL CHECK (chunk_count > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, kb_id) REFERENCES openscience_m4_knowledge.collections(owner_id, kb_id),
    UNIQUE (owner_id, kb_id, doc_id)
);
CREATE TABLE openscience_m4_knowledge.chunks (
    chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL,
    kb_id uuid NOT NULL,
    doc_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 1200),
    terms tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
    FOREIGN KEY (owner_id, kb_id, doc_id)
        REFERENCES openscience_m4_knowledge.documents(owner_id, kb_id, doc_id),
    UNIQUE (owner_id, kb_id, doc_id, ordinal)
);
CREATE INDEX knowledge_chunk_terms ON openscience_m4_knowledge.chunks USING gin (terms);

DO $$
DECLARE
    target regclass;
BEGIN
    FOREACH target IN ARRAY ARRAY['openscience_m4_knowledge.collections'::regclass,
                                   'openscience_m4_knowledge.documents'::regclass,
                                   'openscience_m4_knowledge.chunks'::regclass] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('CREATE POLICY owner_access ON %s TO nexpoly_api
            USING (owner_id = NULLIF(current_setting(''app.user_id'',true),'''')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting(''app.user_id'',true),'''')::uuid)', target);
        EXECUTE format('GRANT SELECT, INSERT ON %s TO nexpoly_api', target);
    END LOOP;
END $$;
COMMIT;
