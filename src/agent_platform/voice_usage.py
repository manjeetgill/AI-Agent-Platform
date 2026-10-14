"""Pre-dispatch spend reservations. Unknown/cancelled calls retain their full hold.

All amounts are estimates in a single configured currency, not customer invoices.
No text/audio/credentials enter this ledger. Local means zero external API fees.
"""
import asyncio
import os
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from . import voice_config
from .voice_failures import VoiceUsageError

active = ContextVar('voice_usage_meter', default=None)
UNITS = {'asr': 'audio_seconds', 'tts': 'characters', 'reasoning': 'tokens'}


@contextmanager
def using(pool, session):
    token = active.set((pool, session))
    try:
        yield
    finally:
        active.reset(token)


async def reserve(stage, maximum):
    context = active.get()
    if context is None:
        return None
    maximum = Decimal(str(maximum))
    if not maximum.is_finite() or maximum < 0:
        raise ValueError('Invalid usage reservation')
    pool, session = context
    config = voice_config.current()
    provider = getattr(config, stage)
    model = config.model if stage == 'reasoning' else getattr(config, stage + '_model')
    if provider == 'sarvam' and stage in {'asr', 'tts'}:
        # The Sarvam adapter pins its transport models; generic Gemini defaults
        # must never select a different rate card or label the resulting usage.
        from .sarvam_speech import MODELS
        model = MODELS[stage]
    if provider == "local" and stage != "reasoning":
        model = Path(os.environ.get("AI_LOCAL_STT_MODEL", "unconfigured")).name if stage == "asr" else "espeak-ng"
    ident = uuid4()
    async with pool.connection() as conn, conn.transaction():
        call = await (await conn.execute('SELECT app_id FROM web_voice_sessions WHERE id=%s', (session,))).fetchone()
        if not call:
            raise ValueError('Voice session unavailable')
        # App lock serializes reservations across sessions, API processes and stages.
        await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (call['app_id'],))
        limits = await (await conn.execute('SELECT * FROM voice_spend_limits WHERE app_id=%s', (call['app_id'],))).fetchone()
        if provider == 'local':
            rate, currency, reference = Decimal(0), limits['currency'] if limits else 'INR', 'local:external-api-fees-only'
        else:
            card = await (await conn.execute('SELECT * FROM voice_rate_cards WHERE provider=%s AND model=%s AND stage=%s AND expires_at>now()', (provider, model, stage))).fetchone()
            if not card or not limits or card['currency'] != limits['currency']:
                raise VoiceUsageError('RATE_CONFIGURATION_REQUIRED', 'Configure current voice rates and application spending limits before paid calls')
            rate, currency, reference = card['per_unit'], card['currency'], card['reference']
        hold = maximum * rate
        if hold:
            used = await (await conn.execute("""SELECT
                coalesce(sum(coalesce(r.actual_amount,l.amount,l.reserved_amount)) FILTER (WHERE l.created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),0) AS daily,
                coalesce(sum(coalesce(r.actual_amount,l.amount,l.reserved_amount)),0) AS monthly
                FROM voice_usage_ledger l LEFT JOIN voice_reconciliations r ON r.ledger_id=l.id WHERE l.app_id=%s AND l.created_at>=date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""", (call['app_id'],))).fetchone()
            if used['daily']+hold > limits['daily'] or used['monthly']+hold > limits['monthly']:
                raise VoiceUsageError('SPENDING_LIMIT', 'Voice spending allowance reached')
        await conn.execute('''INSERT INTO voice_usage_ledger(id,session_id,app_id,stage,provider,model,unit,state,reserved_units,currency,rate,reserved_amount,rate_reference)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'RESERVED',%s,%s,%s,%s,%s)''',
            (ident, session, call['app_id'], stage, provider, model, UNITS[stage], maximum, currency, rate, hold, reference))
    return pool, ident


async def settle(reservation, units=None, response_id=None):
    if reservation is None:
        return
    pool, ident = reservation
    quantity = Decimal(str(units)) if units is not None else None
    if quantity is not None and (not quantity.is_finite() or quantity < 0):
        quantity = None
    response_id = response_id[:200] if isinstance(response_id, str) else None
    async with pool.connection() as conn:
        await conn.execute('''UPDATE voice_usage_ledger SET state=%s,units=%s,
            amount=CASE WHEN %s::numeric IS NULL THEN NULL ELSE %s*rate END,
            provider_response_id=%s,completed_at=now() WHERE id=%s AND state='RESERVED' ''',
            ('DONE' if quantity is not None else 'UNKNOWN', quantity, quantity, quantity, response_id, ident))


async def measured(stage, maximum, operation, quantity):
    reservation = await reserve(stage, maximum)
    try:
        result = await operation()
    except BaseException:
        # Shield accounting cleanup from cancellation. The hold also survives process death.
        await asyncio.shield(settle(reservation))
        raise
    await settle(reservation, quantity(result))
    return result


async def summary(conn, app):
    rows = await (await conn.execute("""SELECT l.currency,l.stage,l.unit,
        count(*) AS attempts,coalesce(sum(l.units),0) AS measured_units,
        count(*) FILTER(WHERE l.state<>'DONE' AND r.ledger_id IS NULL) AS uncertain_attempts,
        coalesce(sum(l.amount),0) AS estimated_amount,
        coalesce(sum(l.reserved_amount) FILTER(WHERE l.state<>'DONE' AND r.ledger_id IS NULL),0) AS held_amount,
        count(r.ledger_id) AS reconciled_attempts,
        coalesce(sum(r.actual_amount),0) AS reconciled_amount,
        coalesce(sum(r.actual_amount-coalesce(l.amount,l.reserved_amount)),0) AS reconciliation_delta,
        coalesce(sum(coalesce(r.actual_amount,l.amount,l.reserved_amount)),0) AS budget_amount
        FROM voice_usage_ledger l LEFT JOIN voice_reconciliations r ON r.ledger_id=l.id
        WHERE l.app_id=%s AND l.created_at>=date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        GROUP BY l.currency,l.stage,l.unit ORDER BY l.currency,l.stage""", (app,))).fetchall()
    return {'period': 'UTC calendar month', 'stages': rows, 'invoice': False,
            'scope': 'API estimates plus reviewed provider charges; excludes hosting, electricity, carrier and taxes. Unreconciled unknown attempts retain holds.'}
