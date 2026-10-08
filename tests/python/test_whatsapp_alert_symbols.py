"""Index display names must survive the app-to-WhatsApp delivery contract."""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_platform.whatsapp_alerts import AlertDelivery, message


def payload(symbol):
    return {'request_key': 'market-alert-fixture', 'exchange': 'NSE', 'symbol': symbol,
            'timeframe': 'live', 'condition': 'price_above', 'target': '22000', 'observed': '22236.35',
            'triggered_at': datetime(2026, 10, 8, 9, 4, 47, tzinfo=UTC)}


@pytest.mark.parametrize('symbol', ['NIFTY 50', 'NIFTY BANK', 'NIFTY FIN SERVICE',
                                   'PNBHOUSING', 'M&M', 'BAJAJ-AUTO'])
def test_exchange_symbols_are_preserved_in_messages(symbol):
    alert = AlertDelivery.model_validate(payload(symbol))
    assert alert.symbol == symbol
    assert f'NSE:{symbol}\n' in message(alert)
    assert '02:34 PM IST' in message(alert)


@pytest.mark.parametrize('symbol', ['', ' NIFTY 50', 'NIFTY 50 ', 'NIFTY  50',
                                   'NIFTY\n50', 'NIFTY\r50', 'NIFTY\t50', '<script>', 'X' * 121])
def test_malformed_symbols_remain_rejected(symbol):
    with pytest.raises(ValidationError):
        AlertDelivery.model_validate(payload(symbol))
