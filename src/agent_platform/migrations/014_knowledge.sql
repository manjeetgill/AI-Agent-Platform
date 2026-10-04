ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish']::text[]);
CREATE TABLE knowledge_documents (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, external_id text NOT NULL, title text NOT NULL, source_label text NOT NULL,
 visibility text NOT NULL CHECK(visibility IN ('private','app')), version integer NOT NULL DEFAULT 1,
 digest text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(app_id,owner_id,external_id)
);
CREATE TABLE knowledge_chunks (
 document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
 position integer NOT NULL, content text NOT NULL,
 search tsvector GENERATED ALWAYS AS (to_tsvector('english',content)) STORED,
 PRIMARY KEY(document_id,position)
);
CREATE INDEX knowledge_chunk_search ON knowledge_chunks USING gin(search);
CREATE INDEX knowledge_document_access ON knowledge_documents(app_id,owner_id,visibility);
CREATE TABLE knowledge_answers (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, request_key text NOT NULL, fingerprint text NOT NULL,
 status text NOT NULL CHECK(status IN ('STARTED','DONE','UNKNOWN')), result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(app_id,owner_id,request_key)
);
