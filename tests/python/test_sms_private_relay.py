import importlib.util
import socket
import threading
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('sms_relay', Path(__file__).resolve().parents[2] / 'deploy/sms_private_relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


@pytest.mark.parametrize('address', ['0.0.0.0','127.0.0.1','169.254.169.254','139.59.64.197','::1'])
def test_rejects_non_bridge_bind(address):
    with pytest.raises(ValueError):
        relay.private_address(address)


def test_private_bind_and_client_filter():
    assert relay.private_address('172.23.0.1') == '172.23.0.1'
    with relay.Relay(('127.0.0.1',0),18082,'172.23.0.0/16') as server:
        assert server.verify_request(None,('172.23.0.5',1234))
        assert not server.verify_request(None,('203.0.113.2',1234))


def test_forwards_opaque_bytes_both_directions_and_stops_on_disconnect():
    a, left = socket.socketpair()
    right, b = socket.socketpair()
    for sock in (a,left,right,b):
        sock.settimeout(2)
    thread = threading.Thread(target=relay.forward,args=(left,right,1))
    try:
        thread.start()
        a.sendall(b'opaque request')
        assert b.recv(100) == b'opaque request'
        b.sendall(b'opaque response')
        assert a.recv(100) == b'opaque response'
        a.close()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        for sock in (a,left,right,b):
            sock.close()
