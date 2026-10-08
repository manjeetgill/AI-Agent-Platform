"""Read-only, app-scoped integration inventory for consuming backends."""

from fastapi import APIRouter, Request

from .business_workspaces import enabled_features
from .service_identity import authenticate


def counts(row):
    return {key: row[key] for key in row}


def install_integration_status(app):
    router = APIRouter(prefix='/v1/integrations', tags=['App integration status'])

    @router.get('/readiness')
    async def readiness(request: Request):
        pool = request.app.state.repository.pool
        claims = await authenticate(pool, request.headers.get('authorization', ''),
                                    request.headers.get('X-External-User-ID'), None)
        app_id, owner = claims['app_id'], claims['user']
        async with pool.connection() as conn:
            channels = await (await conn.execute('''SELECT count(*)::int AS total,
                count(*) FILTER(WHERE enabled)::int AS active,
                count(*) FILTER(WHERE webhook_verified_at IS NOT NULL)::int AS webhook_verified,
                count(*) FILTER(WHERE display_number IS NOT NULL)::int AS sender_identified
                FROM whatsapp_channels WHERE app_id=%s AND owner_id=%s''',
                (app_id, owner))).fetchone()
            route = await (await conn.execute('''SELECT enabled,daily_limit,sender_number,
                require_recipient_verification FROM whatsapp_alert_routes WHERE app_id=%s''',
                (app_id,))).fetchone()
            subscription = await (await conn.execute('''SELECT active,consented_at
                FROM whatsapp_alert_subscriptions WHERE app_id=%s AND owner_id=%s''',
                (app_id, owner))).fetchone()
            calling = await (await conn.execute('''SELECT count(*)::int AS total,
                count(*) FILTER(WHERE enabled)::int AS active,
                count(*) FILTER(WHERE whatsapp_channel_id IS NOT NULL)::int AS number_bound
                FROM calling_agents WHERE app_id=%s AND owner_id=%s''',
                (app_id, owner))).fetchone()
            voice = await (await conn.execute('''SELECT count(*)::int AS total,
                count(*) FILTER(WHERE enabled)::int AS active
                FROM web_voice_agents WHERE app_id=%s AND owner_id=%s''',
                (app_id, owner))).fetchone()
            telephone = await (await conn.execute('''SELECT count(*)::int AS total,
                count(*) FILTER(WHERE enabled)::int AS active,
                count(*) FILTER(WHERE carrier_verified_at IS NOT NULL)::int AS carrier_verified,
                count(*) FILTER(WHERE heartbeat_at>now()-interval '90 seconds')::int AS gateway_online
                FROM telephony_connections WHERE app_id=%s AND owner_id=%s''',
                (app_id, owner))).fetchone()
            memory = await (await conn.execute('''SELECT count(*)::int AS total,
                count(*) FILTER(WHERE status='VALIDATED' AND expires_at>now())::int AS validated
                FROM platform_memories WHERE app_id=%s AND (owner_id=%s OR visibility='app')''',
                (app_id, owner))).fetchone()
            trial = await (await conn.execute('''SELECT expires_at,whatsapp_turns,whatsapp_sends,voice_calls
                FROM channel_trial_policies WHERE app_id=%s''', (app_id,))).fetchone()
        return {
            'app_id': app_id,
            'features_granted': enabled_features(claims['scopes']),
            'scopes': claims['scopes'],
            'components': {
                'whatsapp_support': counts(channels),
                'whatsapp_notifications': {
                    'route_configured': bool(route), 'route_enabled': bool(route and route['enabled']),
                    'sender_number': route['sender_number'] if route else None,
                    'daily_limit': route['daily_limit'] if route else None,
                    'recipient_verification': bool(route and route['require_recipient_verification']),
                    'subscription_active': bool(subscription and subscription['active']),
                    'consented_at': subscription['consented_at'] if subscription else None,
                },
                'whatsapp_calling': counts(calling),
                'internet_voice': counts(voice),
                'telephone_voice': counts(telephone),
                'memory': counts(memory),
            },
            'trial_policy': trial,
            'provider_checks_performed': False,
            'note': ('This inventory reports saved platform state. Provider eligibility, model access, '
                     'live audio and real message delivery require their dedicated readiness/live tests.'),
        }

    app.include_router(router)
