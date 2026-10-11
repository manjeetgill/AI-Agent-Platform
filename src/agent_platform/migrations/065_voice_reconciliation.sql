-- Reviewed provider charges are separate from immutable runtime estimates.
CREATE TABLE voice_reconciliations (
 ledger_id uuid PRIMARY KEY REFERENCES voice_usage_ledger(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 provider text NOT NULL,
 invoice_reference text NOT NULL CHECK(length(invoice_reference) BETWEEN 1 AND 160),
 line_reference text NOT NULL CHECK(length(line_reference) BETWEEN 1 AND 160),
 currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
 actual_amount numeric(20,10) NOT NULL CHECK(actual_amount>=0),
 reconciled_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,provider,invoice_reference,line_reference)
);
CREATE INDEX voice_reconciliations_app ON voice_reconciliations(app_id,reconciled_at);
ALTER TABLE voice_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_reconciliations FORCE ROW LEVEL SECURITY;
CREATE POLICY reconciliation_read ON voice_reconciliations FOR SELECT USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM platform_apps a WHERE a.id=app_id));
CREATE POLICY reconciliation_operator ON voice_reconciliations USING(current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='voice_reconciliations'::regclass))) WITH CHECK(current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='voice_reconciliations'::regclass)));
