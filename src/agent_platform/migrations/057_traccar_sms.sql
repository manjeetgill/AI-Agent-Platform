-- 056 remains immutable. Retain android_local only for historical audit rows;
-- the runtime no longer accepts that retired route or executes its adapter.
ALTER TABLE notification_sms_jobs DROP CONSTRAINT notification_sms_jobs_provider_check;
ALTER TABLE notification_sms_jobs ADD CONSTRAINT notification_sms_jobs_provider_check
 CHECK(provider IN ('mock','smsindiahub','android_local','traccar_local'));
