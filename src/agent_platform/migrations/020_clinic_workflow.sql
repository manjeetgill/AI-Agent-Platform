ALTER TABLE whatsapp_channels ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'RAG' CHECK(execution_mode IN ('RAG','SAHAYAK'));
