import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from agent_platform.chakra_client import (
    ChakraError,
    list_templates,
    phone_details,
    send_message,
    verify_event,
)

CONFIG = {'plugin_id': '83df6ae9-668e-416a-bb3c-3e499b7df924', 'graph_version': 'v26.0',
          'access_token': 'test-only-secret', 'phone_id': '12345678', 'waba_id': '87654321'}


def test_catalogue_paginates_only_on_the_bound_provider_endpoint():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == 'api.chakrahq.com'
        assert request.url.path == '/v1/ext/plugin/whatsapp/api/v26.0/87654321/message_templates'
        assert request.headers['authorization'] == 'Bearer test-only-secret'
        if len(calls) == 1:
            return httpx.Response(200, json={'data': [{'id': '111111'}],
                'paging': {'next': 'https://untrusted.invalid/token-stealer', 'cursors': {'after': 'page2'}}})
        assert request.url.params['after'] == 'page2'
        return httpx.Response(200, json={'data': [{'id': '222222'}]})

    assert asyncio.run(list_templates(CONFIG, transport=httpx.MockTransport(handler))) == [
        {'id': '111111'}, {'id': '222222'}]
    assert len(calls) == 2


def test_catalogue_rejects_bad_configuration_redirects_and_unsanitized_errors():
    with pytest.raises(ChakraError, match='INVALID_CONNECTION'):
        asyncio.run(list_templates({**CONFIG, 'waba_id': '../other-account'}))
    for response in [httpx.Response(302, headers={'Location': 'https://untrusted.invalid'}),
                     httpx.Response(401, json={'secret': 'provider-private-detail'}),
                     httpx.Response(200, json={'_errors': ['provider-private-detail']})]:
        with pytest.raises(ChakraError, match='CATALOGUE_UNAVAILABLE'):
            asyncio.run(list_templates(CONFIG, transport=httpx.MockTransport(lambda request, value=response: value)))


def test_phone_metadata_is_limited_to_the_configured_number():
    def handler(request):
        assert request.url.path.endswith('/'+CONFIG['plugin_id']+'/config')
        return httpx.Response(200, json={'_data': {'whatsappPhoneNumbers': [
            {'id': '99999999', 'display_phone_number': 'other'},
            {'id': CONFIG['phone_id'], 'display_phone_number': '+12345678',
             'verified_name': 'Example clinic', 'private_field': 'do-not-return'}]}})
    assert asyncio.run(phone_details(CONFIG, transport=httpx.MockTransport(handler))) == {
        'display_phone_number': '+12345678', 'verified_name': 'Example clinic'}
    with pytest.raises(ChakraError, match='WRONG_PHONE_NUMBER'):
        asyncio.run(phone_details({**CONFIG, 'phone_id': '22222222'},
                                 transport=httpx.MockTransport(handler)))


def test_send_and_uncertain_outcome_no_retry():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == 'api.chakrahq.com'
        assert request.headers['authorization'] == 'Bearer test-only-secret'
        return httpx.Response(200, json={'_data': {'whatsappMessageId': 'wamid.test'}})

    assert asyncio.run(send_message(CONFIG, '12345678', {'type': 'text'},
                                   transport=httpx.MockTransport(handler))) == 'wamid.test'
    assert len(requests) == 1

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout('sensitive-provider-details', request=request)

    with pytest.raises(ChakraError) as error:
        asyncio.run(send_message(CONFIG, '12345678', {}, transport=httpx.MockTransport(timeout)))
    assert error.value.uncertain
    assert str(error.value) == 'SEND_OUTCOME_UNKNOWN'
    assert len(requests) == 2


def test_destination_binding_before_network():
    with pytest.raises(ChakraError, match='INVALID_CONNECTION'):
        asyncio.run(send_message(CONFIG, '87654321', {}))


def test_webhook_signature_and_account_isolation():
    secret = 's' * 32
    raw = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': '87654321',
                     'changes': [{'field': 'messages', 'value': {
                         'metadata': {'phone_number_id': '12345678'}}}]}]}).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    kwargs = {'phone_id': '12345678', 'waba_id': '87654321'}
    assert verify_event(raw, signature, secret, **kwargs)['entry']
    for bad_raw, bad_sig in [(raw+b' ', signature), (raw, 'sha256='+signature), (raw, '')]:
        with pytest.raises(ChakraError, match='INVALID_SIGNATURE'):
            verify_event(bad_raw, bad_sig, secret, **kwargs)
    with pytest.raises(ChakraError, match='WRONG_WHATSAPP_ACCOUNT'):
        verify_event(raw, signature, secret, phone_id='12345678', waba_id='other')
    with pytest.raises(ChakraError, match='WRONG_PHONE_NUMBER'):
        verify_event(raw, signature, secret, phone_id='other', waba_id='87654321')
