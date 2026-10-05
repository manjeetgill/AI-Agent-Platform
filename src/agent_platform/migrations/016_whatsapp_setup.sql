ALTER TABLE whatsapp_channels ADD COLUMN webhook_verified_at timestamptz;
CREATE TABLE whatsapp_worker_health (
 id text PRIMARY KEY, last_seen timestamptz NOT NULL, model_configured boolean NOT NULL
);
