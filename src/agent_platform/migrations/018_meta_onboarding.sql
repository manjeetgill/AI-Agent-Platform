CREATE TABLE meta_signup_sessions (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, secret_hash text NOT NULL, state text NOT NULL DEFAULT 'CONNECTING',
 token_cipher text, token_expires_at timestamptz, code_hash text UNIQUE,
 waba_id text, phone_id text, business_id text, finish_type text,
 connection_id uuid, error_code text, updated_at timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes',
 lease_until timestamptz, lease_id uuid
);
CREATE INDEX meta_signup_pending ON meta_signup_sessions(state,updated_at);
CREATE TABLE meta_connections (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, waba_id text NOT NULL, phone_id text NOT NULL UNIQUE,
 business_id text, display_number text, business_name text,
 state text NOT NULL, token_cipher text, token_expires_at timestamptz,
 pin_cipher text, registration_attempted_at timestamptz, registration_confirmed_at timestamptz,
 subscription_confirmed_at timestamptz, checked_at timestamptz, error_code text,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX meta_connection_owner ON meta_connections(app_id,owner_id);
CREATE TABLE meta_waba_owners (
 waba_id text PRIMARY KEY,app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,owner_id uuid NOT NULL
);
CREATE TABLE meta_platform_webhook (
 app_id text PRIMARY KEY,callback_url text NOT NULL,verified_at timestamptz,
 subscription_confirmed_at timestamptz,updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE meta_connection_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,session_id uuid,connection_id uuid,event text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE meta_webhook_receipts (
 digest text PRIMARY KEY,created_at timestamptz NOT NULL DEFAULT now()
);
