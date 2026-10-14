-- Control plane reserves sessions; a separately authenticated media process claims them once.
ALTER TABLE web_voice_sessions ADD COLUMN media_remote boolean NOT NULL DEFAULT false;
ALTER TABLE web_voice_sessions ADD COLUMN media_claimed_at timestamptz;
ALTER TABLE web_voice_sessions ADD COLUMN media_heartbeat timestamptz;
ALTER TABLE web_voice_sessions ADD COLUMN media_connected boolean NOT NULL DEFAULT false;
CREATE TABLE voice_media_nonces (
 nonce uuid PRIMARY KEY, expires_at timestamptz NOT NULL
);
CREATE INDEX voice_media_nonces_expiry ON voice_media_nonces(expires_at);
