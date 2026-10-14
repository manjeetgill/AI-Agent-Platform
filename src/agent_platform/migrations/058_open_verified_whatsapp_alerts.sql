-- Retire the single-user pilot gate. Every recipient must still prove control
-- of their own number and consent before receiving alerts.
UPDATE whatsapp_alert_routes
SET require_recipient_verification=true
WHERE require_recipient_verification=false;
ALTER TABLE whatsapp_alert_routes DROP COLUMN allowed_external_user;
