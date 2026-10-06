-- Explicit pilot policies. Existing non-pilot apps retain their existing quotas.
CREATE TABLE channel_trial_policies (
 app_id uuid PRIMARY KEY REFERENCES platform_apps(id) ON DELETE CASCADE,
 expires_at timestamptz NOT NULL,
 whatsapp_turns integer NOT NULL CHECK(whatsapp_turns BETWEEN 0 AND 100000),
 whatsapp_sends integer NOT NULL CHECK(whatsapp_sends BETWEEN 0 AND 100000),
 voice_calls integer NOT NULL CHECK(voice_calls BETWEEN 0 AND 100000)
);
CREATE TABLE channel_trial_usage (
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 day date NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
 resource text NOT NULL CHECK(resource IN ('whatsapp_turns','whatsapp_sends','voice_calls')),
 units integer NOT NULL DEFAULT 0 CHECK(units>=0),
 PRIMARY KEY(app_id,day,resource)
);
CREATE TABLE channel_trial_reservations (
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 resource text NOT NULL,
 operation_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(app_id,resource,operation_id)
);
