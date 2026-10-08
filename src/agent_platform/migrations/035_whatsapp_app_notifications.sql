-- Capture a trusted display number from Meta onboarding or signed webhook metadata.
ALTER TABLE whatsapp_channels ADD COLUMN display_number text
 CHECK(display_number IS NULL OR display_number ~ '^[+][1-9][0-9]{7,14}$');

UPDATE whatsapp_channels ch SET display_number=m.display_number
 FROM meta_connections m
 WHERE m.phone_id=ch.phone_id AND m.app_id=ch.app_id AND m.owner_id=ch.owner_id
 AND m.display_number ~ '^[+][1-9][0-9]{7,14}$';

CREATE INDEX whatsapp_channel_display_number ON whatsapp_channels(display_number)
 WHERE display_number IS NOT NULL;

ALTER TABLE whatsapp_alert_deliveries
 ADD COLUMN kind text NOT NULL DEFAULT 'market_alert'
 CHECK(kind IN ('market_alert','notification')),
 ADD COLUMN category text
 CHECK(category IS NULL OR category ~ '^[a-z][a-z0-9_.-]{0,49}$');
CREATE INDEX whatsapp_alert_delivery_kind
 ON whatsapp_alert_deliveries(app_id,kind,created_at DESC);
