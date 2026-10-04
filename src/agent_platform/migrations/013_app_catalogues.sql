CREATE TABLE platform_app_policies (
 app_id uuid PRIMARY KEY REFERENCES platform_apps(id) ON DELETE CASCADE,
 planning_calls_per_day integer NOT NULL DEFAULT 500 CHECK(planning_calls_per_day BETWEEN 0 AND 100000),
 user_planning_calls_per_day integer NOT NULL DEFAULT 50 CHECK(user_planning_calls_per_day BETWEEN 0 AND 10000),
 max_plan_steps integer NOT NULL DEFAULT 10 CHECK(max_plan_steps BETWEEN 1 AND 30)
);
CREATE TABLE platform_agent_templates (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 slug text NOT NULL, version integer NOT NULL CHECK(version>0), blueprint jsonb NOT NULL,
 definition_hash text NOT NULL, enabled boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(app_id,slug,version)
);
CREATE INDEX platform_template_catalogue ON platform_agent_templates(app_id,slug,version DESC);
