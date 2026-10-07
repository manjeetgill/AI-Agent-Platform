-- A consuming app can use an operator-approved sender without owning its channel.
ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write',
 'telephony:read','telephony:write','memory:read','memory:write','memory:review',
 'alerts:send']::text[]);

CREATE TABLE whatsapp_alert_routes (
 app_id uuid PRIMARY KEY REFERENCES platform_apps(id) ON DELETE CASCADE,
 channel_id uuid NOT NULL REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 sender_number text CHECK(sender_number IS NULL OR sender_number ~ '^[+][1-9][0-9]{7,14}$'),
 allowed_external_user text,
 enabled boolean NOT NULL DEFAULT false,
 daily_limit integer NOT NULL DEFAULT 20 CHECK(daily_limit BETWEEN 1 AND 1000),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE whatsapp_alert_subscriptions (
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 recipient text NOT NULL CHECK(recipient ~ '^[1-9][0-9]{7,14}$'),
 consented_at timestamptz NOT NULL,
 active boolean NOT NULL DEFAULT true,
 PRIMARY KEY(app_id,owner_id)
);
CREATE TABLE whatsapp_alert_deliveries (
 id uuid PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 channel_id uuid NOT NULL REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 request_key text NOT NULL,
 recipient text NOT NULL,
 body text NOT NULL CHECK(length(body) BETWEEN 1 AND 1000),
 status text NOT NULL CHECK(status IN ('QUEUED','SENDING','ACCEPTED','UNKNOWN','FAILED','CANCELLED')),
 provider_id text,
 error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,request_key)
);
CREATE INDEX whatsapp_alert_delivery_queue ON whatsapp_alert_deliveries(status,created_at);
CREATE INDEX whatsapp_alert_delivery_owner ON whatsapp_alert_deliveries(app_id,owner_id,created_at DESC);
