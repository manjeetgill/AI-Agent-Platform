"""App-scoped service setup preferences; both routes are operated by the platform.

A request is not a subscription purchase or a runtime route switch. Activation
requires operator provisioning and a separate live verification of that route.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, model_validator

from .automation import scope
from .service_identity import authenticate
from .task_api import credential_contract
from .tasks import Strict

OPTIONS = {
    'speech': {'title': 'Speech', 'platform': ['local'], 'provider': ['sarvam'],
               'ours': 'Speech recognition and generation on our servers.',
               'extra': 'Sarvam speech operated by our platform; additional usage charges apply.'},
    'email': {'title': 'Email', 'platform': ['smtp'], 'provider': ['resend', 'brevo'],
              'ours': 'Email through our own mail server.',
              'extra': 'Resend or Brevo delivery operated by our platform; additional charges may apply.'},
    'whatsapp': {'title': 'WhatsApp', 'platform': ['meta'], 'provider': ['chakra'],
                 'ours': 'Our direct Meta connection, without a Chakra subscription.',
                 'extra': 'Chakra integration operated by our platform; additional provider charges may apply.'},
}


class Choice(Strict):
    capability: Literal['speech', 'email', 'whatsapp']
    route: Literal['platform', 'provider']
    provider: Literal['local', 'smtp', 'meta', 'sarvam', 'resend', 'brevo', 'chakra']
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid_route(self):
        if self.provider not in OPTIONS[self.capability][self.route]:
            raise ValueError('Provider does not match this service option')
        return self


async def inventory(pool, claims):
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, claims)
        rows = await (await conn.execute('SELECT capability,route,provider,revision,updated_at FROM platform_service_choices WHERE app_id=%s AND tenant_id=%s',
                                        (claims['app_id'],claims['tenant']))).fetchall()
    saved = {r['capability']:r for r in rows}
    return {'services': [{**value, 'capability': name, 'choice': saved.get(name),
                          'setup_status': 'SETUP_REQUESTED' if name in saved else 'NOT_SELECTED',
                          'price': None, 'price_status': 'QUOTE_REQUIRED', 'activated': False}
                         for name,value in OPTIONS.items()],
            'account_owner': 'platform', 'customer_provider_account_required': False,
            'labels': {'platform': 'Use our service', 'provider': 'Use a provider-backed service'},
            'note': 'Saving a preference requests setup. It does not change an active connection, buy a plan or start billing. The platform will confirm pricing and verify setup before activation.',
            'whatsapp_note': 'Business number authorization is still required. Applicable Meta charges remain with either option.'}


async def save(pool, claims, body):
    async with pool.connection() as conn, conn.transaction():
        await scope(conn, claims)
        # Serialize concurrent first writes as well as revisions of an existing choice.
        app = await (await conn.execute('SELECT id FROM platform_apps WHERE id=%s AND tenant_id=%s FOR UPDATE',
                                       (claims['app_id'],claims['tenant']))).fetchone()
        if not app:
            raise HTTPException(404, 'Application not found')
        prior = await (await conn.execute('SELECT * FROM platform_service_choices WHERE app_id=%s AND tenant_id=%s AND capability=%s',
                                         (claims['app_id'],claims['tenant'],body.capability))).fetchone()
        revision = prior['revision'] if prior else 0
        if prior and prior['route']==body.route and prior['provider']==body.provider and body.expected_revision in {revision,revision-1}:
            return {'saved':True,'revision':revision,'status':'SETUP_REQUESTED','activated':False}
        if body.expected_revision != revision:
            raise HTTPException(409, 'Service preference changed; refresh before saving')
        await conn.execute('''INSERT INTO platform_service_choices(tenant_id,app_id,capability,route,provider,revision)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,capability) DO UPDATE SET
            route=excluded.route,provider=excluded.provider,revision=excluded.revision,updated_at=now()''',
            (claims['tenant'],claims['app_id'],body.capability,body.route,body.provider,revision+1))
        await conn.execute('''INSERT INTO platform_service_choice_events(tenant_id,app_id,capability,route,provider,revision,actor)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',
            (claims['tenant'],claims['app_id'],body.capability,body.route,body.provider,revision+1,claims['principal']))
    return {'saved':True,'revision':revision+1,'status':'SETUP_REQUESTED','activated':False}


def install(app):
    router = APIRouter(prefix='/v1/integrations/service-options',dependencies=[Depends(credential_contract)])

    async def claims(request, permission):
        return await authenticate(request.app.state.repository.pool,request.headers.get('authorization'),
                                  request.headers.get('X-External-User-ID'),permission)

    @router.get('')
    async def read(request: Request):
        return await inventory(request.app.state.repository.pool,await claims(request,'services:read'))

    @router.post('')
    async def write(body: Choice, request: Request):
        return await save(request.app.state.repository.pool,await claims(request,'services:write'),body)

    app.include_router(router)
