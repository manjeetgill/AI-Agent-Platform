-- Dynamic WhatsApp provider selection per channel (ChakraHQ BSP vs Direct Meta Cloud API)
ALTER TABLE whatsapp_channels
    ADD COLUMN IF NOT EXISTS provider VARCHAR(20) NOT NULL DEFAULT 'chakra'
    CHECK (provider IN ('chakra', 'meta'));

-- Backfill existing channels that are connected via Direct Meta
UPDATE whatsapp_channels c
SET provider = 'meta'
WHERE EXISTS (
    SELECT 1 FROM meta_connections m WHERE m.phone_id = c.phone_id
) AND NOT EXISTS (
    SELECT 1 FROM chakra_customer_connections ch WHERE ch.phone_id = c.phone_id
);
