import asyncio

from agent_platform import chakra_client, whatsapp_outbound


def test_only_approved_static_templates_are_offered_for_review(monkeypatch):
    async def catalogue(config):
        assert config['provider'] == 'chakra'
        return [
            {'id': '123456', 'name': 'feedback', 'language': 'en', 'status': 'APPROVED',
             'components': [{'type': 'BODY', 'text': 'Reply FEEDBACK.'}]},
            {'id': '123457', 'name': 'pending', 'language': 'en', 'status': 'PENDING',
             'components': [{'type': 'BODY', 'text': 'Not approved.'}]},
            {'id': '123458', 'name': 'parameters', 'language': 'en', 'status': 'APPROVED',
             'components': [{'type': 'BODY', 'text': 'Hello {{1}}'}]},
        ]

    async def forbidden(*args, **kwargs):
        raise AssertionError('A Chakra credential must never be sent to Meta')

    monkeypatch.setattr(chakra_client, 'list_templates', catalogue)
    monkeypatch.setattr(whatsapp_outbound, 'graph', forbidden)
    options = asyncio.run(whatsapp_outbound.template_options({'provider': 'chakra', 'waba_id': '12345678'}))
    assert [(t['id'], t['status']) for t in options] == [('123456', 'APPROVED'), ('123457', 'PENDING')]
    assert asyncio.run(whatsapp_outbound.templates({'provider': 'chakra', 'waba_id': '12345678'})) == [
        {'id': '123456', 'name': 'feedback', 'language': 'en', 'text': 'Reply FEEDBACK.'}]


def test_template_send_uses_existing_single_attempt_chakra_transport(monkeypatch):
    calls = []

    async def sender(config, phone, payload):
        calls.append(payload)
        assert phone == '12345678'
        return 'wamid.test-template'

    async def forbidden(*args, **kwargs):
        raise AssertionError('A Chakra credential must never be sent to Meta')

    monkeypatch.setattr(chakra_client, 'send_message', sender)
    monkeypatch.setattr(whatsapp_outbound, 'graph', forbidden)
    assert asyncio.run(whatsapp_outbound.send_template({'provider': 'chakra'}, '12345678', '19999999999',
        {'name': 'feedback', 'language': 'en'})) == 'wamid.test-template'
    assert calls == [{'messaging_product': 'whatsapp', 'to': '19999999999', 'type': 'template',
                      'template': {'name': 'feedback', 'language': {'code': 'en'}}}]
