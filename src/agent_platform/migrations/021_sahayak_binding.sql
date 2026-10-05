CREATE TABLE sahayak_channel_bridges (
 channel_id uuid PRIMARY KEY REFERENCES whatsapp_channels(id) ON DELETE CASCADE,
 config_cipher text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
