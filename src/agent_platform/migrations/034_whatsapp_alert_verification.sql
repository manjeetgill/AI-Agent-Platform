-- Public alert routes require proof that the signed-in app user controls the recipient.
ALTER TABLE whatsapp_alert_routes
 ADD COLUMN require_recipient_verification boolean NOT NULL DEFAULT true;

-- Preserve explicitly restricted operator pilots while making new routes secure by default.
UPDATE whatsapp_alert_routes SET require_recipient_verification=false
 WHERE allowed_external_user IS NOT NULL;

CREATE TABLE whatsapp_alert_verifications (
 id uuid PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 channel_id uuid NOT NULL REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 recipient text NOT NULL CHECK(recipient ~ '^[1-9][0-9]{7,14}$'),
 code_hash text NOT NULL CHECK(code_hash ~ '^[a-f0-9]{64}$'),
 state text NOT NULL DEFAULT 'PENDING'
  CHECK(state IN ('PENDING','VERIFIED','EXPIRED','CANCELLED','CONSUMED')),
 expires_at timestamptz NOT NULL,
 verified_at timestamptz,
 consumed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX whatsapp_alert_verification_owner
 ON whatsapp_alert_verifications(app_id,owner_id,created_at DESC);
CREATE INDEX whatsapp_alert_verification_inbound
 ON whatsapp_alert_verifications(channel_id,recipient,state,expires_at);
