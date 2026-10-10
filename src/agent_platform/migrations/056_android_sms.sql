-- Extend the existing isolated outbox; no new roles or public routes.
ALTER TABLE notification_sms_jobs DROP CONSTRAINT notification_sms_jobs_provider_check;
ALTER TABLE notification_sms_jobs ADD CONSTRAINT notification_sms_jobs_provider_check
 CHECK(provider IN ('mock','smsindiahub','android_local'));
