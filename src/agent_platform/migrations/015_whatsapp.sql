ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write']::text[]);
CREATE TABLE whatsapp_channels (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, phone_id text UNIQUE NOT NULL, name text NOT NULL,
 documents uuid[] NOT NULL, enabled boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE whatsapp_conversations (
 id uuid PRIMARY KEY, channel_id uuid NOT NULL REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 customer text NOT NULL, mode text NOT NULL DEFAULT 'AUTO' CHECK(mode IN ('AUTO','HUMAN')),
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(channel_id,customer)
);
CREATE TABLE whatsapp_messages (
 id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES whatsapp_conversations(id) ON DELETE CASCADE,
 provider_id text UNIQUE NOT NULL, body text NOT NULL, received_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('PENDING','GENERATING','READY','SENDING','SENT','UNKNOWN','HANDOFF','CANCELLED')),
 reply text, human_reply boolean NOT NULL DEFAULT false, evidence jsonb, outbound_id text UNIQUE, delivery text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX whatsapp_pending ON whatsapp_messages(status,updated_at);
CREATE INDEX whatsapp_history ON whatsapp_messages(conversation_id,received_at,id);
CREATE TABLE whatsapp_audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 channel_id uuid NOT NULL REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 conversation_id uuid, event text NOT NULL, actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
-- No FK: inserting this fence must not wait on a locked message row. Contains only an opaque UUID.
CREATE TABLE whatsapp_dispatches (message_id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
