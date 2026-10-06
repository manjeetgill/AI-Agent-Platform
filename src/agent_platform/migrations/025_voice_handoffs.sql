CREATE TABLE web_voice_handoffs (
 id uuid PRIMARY KEY,
 token_hash text UNIQUE NOT NULL,
 message_id uuid UNIQUE NOT NULL REFERENCES whatsapp_messages(id) ON DELETE CASCADE,
 conversation_id uuid NOT NULL REFERENCES whatsapp_conversations(id) ON DELETE CASCADE,
 agent_id uuid NOT NULL REFERENCES web_voice_agents(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '15 minutes',
 session_id uuid UNIQUE REFERENCES web_voice_sessions(id) ON DELETE SET NULL,
 consumed_at timestamptz
);
ALTER TABLE web_voice_sessions ADD COLUMN handoff_id uuid REFERENCES web_voice_handoffs(id) ON DELETE SET NULL;
CREATE INDEX web_voice_handoff_expiry ON web_voice_handoffs(expires_at);
