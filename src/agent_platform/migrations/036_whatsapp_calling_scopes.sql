-- WhatsApp calling is a separately granted product capability from text support.
ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write',
 'calling:read','calling:write','telephony:read','telephony:write',
 'memory:read','memory:write','memory:review','alerts:send']::text[]);
