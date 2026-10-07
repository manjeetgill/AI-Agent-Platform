ALTER TABLE whatsapp_alert_deliveries ADD COLUMN delivery text
 CHECK(delivery IN ('sent','delivered','read','failed'));
CREATE INDEX whatsapp_alert_provider_receipt ON whatsapp_alert_deliveries(channel_id,provider_id)
 WHERE provider_id IS NOT NULL;
