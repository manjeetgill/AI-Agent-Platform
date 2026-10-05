CREATE TABLE whatsapp_outbound (
 id uuid PRIMARY KEY,
 channel_id uuid NOT NULL REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 request_key text NOT NULL,
 recipient text NOT NULL,
 template jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('DRAFT','QUEUED','SENDING','ACCEPTED','UNKNOWN','FAILED','CANCELLED')),
 approved_at timestamptz,
 expires_at timestamptz NOT NULL DEFAULT now()+interval '10 minutes',
 outbound_id text UNIQUE,
 delivery text,
 error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(channel_id,request_key)
);
CREATE INDEX whatsapp_outbound_queue ON whatsapp_outbound(status,created_at);
CREATE INDEX whatsapp_outbound_history ON whatsapp_outbound(channel_id,created_at,id);
ALTER TABLE whatsapp_conversations ADD COLUMN opted_out boolean NOT NULL DEFAULT false;
