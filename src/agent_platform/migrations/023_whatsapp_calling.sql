-- Preserve historic phone-call records, but never reactivate a retired connection.
UPDATE calling_agents SET enabled=false;
ALTER TABLE calling_agents ADD COLUMN whatsapp_channel_id uuid REFERENCES whatsapp_channels(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX calling_whatsapp_number ON calling_agents(whatsapp_channel_id) WHERE whatsapp_channel_id IS NOT NULL;
ALTER TABLE calling_turns ADD COLUMN reply jsonb;
CREATE TABLE whatsapp_call_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 agent_id uuid NOT NULL REFERENCES calling_agents(id) ON DELETE CASCADE,
 provider_call_id text NOT NULL,
 event_hash text NOT NULL,
 payload jsonb NOT NULL,
 state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','PROCESSING','DONE','UNKNOWN')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(agent_id,event_hash)
);
CREATE INDEX whatsapp_call_events_pending ON whatsapp_call_events(state,id);
CREATE TABLE whatsapp_call_requests (
 id uuid PRIMARY KEY,
 agent_id uuid NOT NULL REFERENCES calling_agents(id) ON DELETE CASCADE,
 recipient text NOT NULL,
 idempotency_key text NOT NULL,
 state text NOT NULL CHECK(state IN ('AWAITING_APPROVAL','QUEUED','SENDING','SUBMITTED','CONNECTED','COMPLETED','FAILED','UNKNOWN')),
 provider_call_id text,
 configuration_hash text NOT NULL,
 approved_by text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(agent_id,idempotency_key)
);
CREATE INDEX whatsapp_call_requests_queue ON whatsapp_call_requests(state,created_at);
CREATE TABLE whatsapp_call_worker_health (
 id text PRIMARY KEY, last_seen timestamptz NOT NULL, media_ready boolean NOT NULL
);
