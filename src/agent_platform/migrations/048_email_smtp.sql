ALTER TABLE notification_email_jobs DROP CONSTRAINT notification_email_jobs_provider_check;
ALTER TABLE notification_email_jobs ADD CONSTRAINT notification_email_jobs_provider_check
 CHECK (provider IN ('mock','resend','brevo','smtp'));
