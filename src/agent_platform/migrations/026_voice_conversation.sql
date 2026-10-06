ALTER TABLE web_voice_agents ADD COLUMN language text NOT NULL DEFAULT 'auto' CHECK(language IN ('auto','en','hi','hinglish'));
