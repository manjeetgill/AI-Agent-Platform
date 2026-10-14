-- Monetary amounts are operator-reviewed estimates, never payment instructions.
CREATE TABLE voice_rate_cards (
 provider text NOT NULL, model text NOT NULL, stage text NOT NULL CHECK(stage IN ('asr','tts','reasoning')),
 currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
 per_unit numeric(20,10) NOT NULL CHECK(per_unit>=0), reference text NOT NULL,
 verified_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 PRIMARY KEY(provider,model,stage)
);
CREATE TABLE voice_spend_limits (
 app_id uuid PRIMARY KEY REFERENCES platform_apps(id) ON DELETE CASCADE,
 currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
 daily numeric(20,10) NOT NULL CHECK(daily>=0), monthly numeric(20,10) NOT NULL CHECK(monthly>=daily)
);
CREATE TABLE voice_usage_ledger (
 id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES web_voice_sessions(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 stage text NOT NULL CHECK(stage IN ('asr','tts','reasoning')),
 provider text NOT NULL, model text NOT NULL, unit text NOT NULL,
 state text NOT NULL CHECK(state IN ('RESERVED','DONE','UNKNOWN')),
 reserved_units numeric(20,6) NOT NULL CHECK(reserved_units>=0), units numeric(20,6),
 currency text NOT NULL, rate numeric(20,10) NOT NULL CHECK(rate>=0),
 reserved_amount numeric(20,10) NOT NULL CHECK(reserved_amount>=0), amount numeric(20,10),
 rate_reference text NOT NULL, provider_response_id text, created_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz,
 CHECK(units IS NULL OR units>=0), CHECK(amount IS NULL OR amount>=0)
);
CREATE INDEX voice_usage_app_time ON voice_usage_ledger(app_id,created_at);
CREATE INDEX voice_usage_session ON voice_usage_ledger(session_id,created_at);
