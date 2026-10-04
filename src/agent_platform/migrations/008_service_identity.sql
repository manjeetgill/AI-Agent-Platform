CREATE TABLE platform_organisations (
 id text PRIMARY KEY, name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE platform_apps (
 id uuid PRIMARY KEY, tenant_id text NOT NULL REFERENCES platform_organisations(id),
 name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE platform_service_keys (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id),
 secret_hash text NOT NULL, scopes text[] NOT NULL,
 expires_at timestamptz NOT NULL, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(scopes <@ ARRAY['tasks:read','tasks:write','tasks:approve']::text[])
);
CREATE TABLE platform_external_users (
 app_id uuid NOT NULL REFERENCES platform_apps(id), external_id text NOT NULL,
 owner_id uuid NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(app_id,external_id)
);
