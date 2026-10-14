ALTER TABLE web_voice_turns DROP CONSTRAINT web_voice_turns_state_check;
ALTER TABLE web_voice_turns ADD CONSTRAINT web_voice_turns_state_check
 CHECK (state IN ('STARTED','DONE','UNKNOWN','CANCELLED'));
