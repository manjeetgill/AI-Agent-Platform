-- NULL means no application-level daily cap. Meta and trial limits still apply.
ALTER TABLE whatsapp_alert_routes ALTER COLUMN daily_limit DROP NOT NULL;
UPDATE whatsapp_alert_routes SET daily_limit=NULL
WHERE app_id IN (SELECT id FROM platform_apps WHERE name='MyAlgoDesk-Alerts');

-- Remove only this app's WhatsApp-send trial cap and expiry. Other pilot
-- resources and other apps retain their existing policies.
ALTER TABLE channel_trial_policies ALTER COLUMN whatsapp_sends DROP NOT NULL;
UPDATE channel_trial_policies SET whatsapp_sends=NULL
WHERE app_id IN (SELECT id FROM platform_apps WHERE name='MyAlgoDesk-Alerts');
