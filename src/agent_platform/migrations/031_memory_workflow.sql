-- Opt-in capture and bounded local semantic indexes. No model weights or external uploads.
CREATE TABLE memory_settings (
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, capture boolean NOT NULL DEFAULT false,
 retention_days integer NOT NULL DEFAULT 30 CHECK(retention_days BETWEEN 1 AND 366),
 daily_candidates integer NOT NULL DEFAULT 25 CHECK(daily_candidates BETWEEN 0 AND 100),
 revision integer NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(app_id,owner_id)
);
ALTER TABLE platform_memories ADD COLUMN origin_answer_id uuid REFERENCES knowledge_answers(id) ON DELETE CASCADE;
CREATE TABLE memory_vectors (
 memory_id uuid PRIMARY KEY REFERENCES platform_memories(id) ON DELETE CASCADE,
 version integer NOT NULL, model text NOT NULL, embedding double precision[] NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE memory_examples (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, memory_id uuid NOT NULL REFERENCES platform_memories(id) ON DELETE CASCADE,
 memory_version integer NOT NULL, question text NOT NULL, answer text NOT NULL,
 status text NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','APPROVED','REVOKED')),
 consent_ref text NOT NULL, reviewer text, evidence_ref text,
 version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_examples_scope ON memory_examples(app_id,owner_id,status);
CREATE TABLE memory_evaluations (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, request_key text NOT NULL, fingerprint text NOT NULL,
 example_id uuid NOT NULL REFERENCES memory_examples(id) ON DELETE CASCADE,
 example_version integer NOT NULL, state text NOT NULL CHECK(state IN ('STARTED','DONE','UNKNOWN')),
 result jsonb, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,request_key)
);
