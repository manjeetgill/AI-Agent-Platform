-- Support approved WhatsApp templates outside the 24-hour conversation window.
ALTER TABLE whatsapp_alert_routes
    ADD COLUMN IF NOT EXISTS template_name VARCHAR(128),
    ADD COLUMN IF NOT EXISTS template_language VARCHAR(10) DEFAULT 'en_US';
