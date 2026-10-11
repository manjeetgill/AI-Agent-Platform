ALTER TABLE web_voice_agents ADD COLUMN deleted_at timestamptz;
CREATE INDEX web_voice_agents_live ON web_voice_agents(app_id,owner_id) WHERE deleted_at IS NULL;
