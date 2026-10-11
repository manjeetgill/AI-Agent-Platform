-- Specialist delegation shares one authorized session, deadline and spend budget.
ALTER TABLE web_voice_sessions ADD COLUMN specialist_snapshot jsonb;
ALTER TABLE web_voice_sessions ADD COLUMN specialist_hops integer NOT NULL DEFAULT 0 CHECK(specialist_hops BETWEEN 0 AND 3);
