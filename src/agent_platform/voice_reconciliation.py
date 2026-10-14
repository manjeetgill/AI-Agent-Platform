"""Operator-reviewed provider invoice import, never a payment or automatic refund.

Each line must identify its original ledger entry and match its application,
provider, model, stage, currency and response ID (if one was recorded). Existing
estimates are retained. Corrections to an imported line require operator review.
"""
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from .tasks import Strict


class Charge(Strict):
    ledger_id: UUID
    app_id: UUID
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    stage: str = Field(pattern='^(asr|tts|reasoning)$')
    currency: str = Field(pattern='^[A-Z]{3}$')
    actual_amount: Decimal = Field(ge=0, max_digits=20, decimal_places=10)
    invoice_reference: str = Field(min_length=1, max_length=160)
    line_reference: str = Field(min_length=1, max_length=160)
    provider_response_id: str | None = Field(default=None, max_length=200)


async def reconcile(conn, lines: list[Charge]):
    if not 1 <= len(lines) <= 10000:
        raise ValueError('Import between 1 and 10000 reviewed invoice lines')
    # Match reservation lock order, deterministic across multi-app imports.
    # Only a migration/operator-owned connection has write permission via RLS.
    async with conn.transaction():
        for app in sorted({str(line.app_id) for line in lines}):
            await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (app,))
        changed = replayed = 0
        for line in lines:
            row = await (await conn.execute('SELECT * FROM voice_usage_ledger WHERE id=%s FOR UPDATE', (line.ledger_id,))).fetchone()
            if not row or any(str(row[k]) != str(getattr(line, k)) for k in ('app_id', 'provider', 'model', 'stage', 'currency')):
                raise ValueError('Invoice line does not match ledger identity')
            if row['state'] == 'RESERVED':
                raise ValueError('An active reservation cannot be reconciled')
            if row['provider_response_id'] and row['provider_response_id'] != line.provider_response_id:
                raise ValueError('Invoice response ID does not match ledger')
            old = await (await conn.execute('SELECT * FROM voice_reconciliations WHERE ledger_id=%s', (line.ledger_id,))).fetchone()
            if old:
                if any(old[k] != getattr(line, k) for k in ('invoice_reference', 'line_reference', 'actual_amount')):
                    raise ValueError('Previously reconciled charge differs; explicit correction review required')
                replayed += 1
                continue
            await conn.execute('''INSERT INTO voice_reconciliations(ledger_id,app_id,provider,invoice_reference,line_reference,currency,actual_amount)
                VALUES(%s,%s,%s,%s,%s,%s,%s)''', (line.ledger_id,line.app_id,line.provider,line.invoice_reference,line.line_reference,line.currency,line.actual_amount))
            changed += 1
        return {'imported': changed, 'unchanged': replayed, 'payment_made': False}
