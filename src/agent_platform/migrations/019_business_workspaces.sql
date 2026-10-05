-- A business receives its own app boundary, including documents, channels and quotas.
CREATE TABLE platform_business_workspaces (
 id uuid PRIMARY KEY,
 parent_app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 external_id text NOT NULL,
 name text NOT NULL,
 app_id uuid NOT NULL UNIQUE REFERENCES platform_apps(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(parent_app_id,external_id)
);
CREATE INDEX platform_business_owner ON platform_business_workspaces(parent_app_id,owner_id,id);
CREATE TABLE chakra_customer_connections (
 id uuid PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 plugin_id uuid NOT NULL UNIQUE,
 phone_id text NOT NULL UNIQUE,
 waba_id text NOT NULL,
 config_cipher text NOT NULL,
 state text NOT NULL DEFAULT 'CONNECTED' CHECK(state IN ('CONNECTED','DISCONNECTED')),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chakra_customer_owner ON chakra_customer_connections(app_id,owner_id);
CREATE TABLE chakra_signup_sessions (
 id uuid PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 state text NOT NULL DEFAULT 'CREATING',
 token_cipher text,
 candidate_plugin_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '55 minutes',
 UNIQUE(app_id,owner_id,id)
);
CREATE INDEX chakra_signup_owner ON chakra_signup_sessions(app_id,owner_id,created_at);
