-- Dynamic WhatsApp provider selection per channel (ChakraHQ BSP vs Direct Meta Cloud API)
ALTER TABLE whatsapp_channels
    ADD COLUMN IF NOT EXISTS provider VARCHAR(20) NOT NULL DEFAULT 'chakra'
    CHECK (provider IN ('chakra', 'meta'));
