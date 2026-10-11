-- Recovery approval is operator-owned; clients can consume it once, not assert ownership.
ALTER TABLE chakra_signup_sessions
 ADD COLUMN recovery_review jsonb,
 ADD COLUMN recovery_consumed_at timestamptz,
 ADD COLUMN recovery_history jsonb NOT NULL DEFAULT '[]'::jsonb;
