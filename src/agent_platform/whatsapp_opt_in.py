"""Handle an explicit recipient START without reactivating the conversation bot."""


async def consume_alert_start(conn, channel_id, conversation_id, message_id, body, received_at):
    if body.strip().lower() != 'start':
        return False
    route = await (await conn.execute(
        'SELECT 1 FROM whatsapp_alert_routes WHERE channel_id=%s AND enabled LIMIT 1',
        (channel_id,),
    )).fetchone()
    if not route:
        return False
    # The webhook calls this only after inserting a new provider message, in the same transaction.
    await conn.execute("UPDATE whatsapp_messages SET status='CANCELLED',updated_at=now() WHERE id=%s",
                       (message_id,))
    # Ignore a START older than (or simultaneous with) a STOP, even if the webhook arrives late.
    await conn.execute('''UPDATE whatsapp_conversations SET opted_out=false,updated_at=now()
        WHERE id=%s AND channel_id=%s AND NOT EXISTS (SELECT 1 FROM whatsapp_messages
            WHERE conversation_id=%s AND received_at>=%s
            AND lower(trim(body)) IN ('stop','unsubscribe'))''',
        (conversation_id, channel_id, conversation_id, received_at))
    await conn.execute('''INSERT INTO whatsapp_audit(channel_id,conversation_id,event,actor)
        VALUES(%s,%s,'recipient:start','provider')''', (channel_id, conversation_id))
    return True


async def subscription_blocker(conn, channel_id, recipient, sender_number):
    row = await (await conn.execute(
        'SELECT opted_out FROM whatsapp_conversations WHERE channel_id=%s AND customer=%s',
        (channel_id, recipient),
    )).fetchone()
    sender = sender_number or 'the connected WhatsApp sender'
    if row and row['opted_out']:
        return f'This number stopped WhatsApp messages. Send START from this number to {sender}, then enable alerts again.'
    if row:
        return f'The WhatsApp conversation has expired. Send Hi from this number to {sender}, then enable alerts again.'
    return f'No message received from this number. Include its country code and send Hi from that same number to {sender}, then enable alerts again.'
