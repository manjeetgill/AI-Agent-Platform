import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from agent_platform.chakra_client import ChakraError, send_message, verify_event

CONFIG = {'plugin_id': '83df6ae9-668e-416a-bb3c-3e499b7df924', 'graph_version': 'v26.0',
          'access_token': 'test-only-secret', 'phone_id': '12345678'}


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
