"""Private Docker bridge to an SSH remote forward; never binds a public address.

Carries opaque authenticated Traccar HTTP. Does not log or retain payloads.
Run behind a restricted SSH key; the upstream is always local to this server.
"""
import argparse
import ipaddress
import select
import socket
import socketserver
import threading


def private_address(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in ipaddress.ip_network(net) for net in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Bind only to a private Docker bridge address')
    return str(address)


def forward(left, right, idle_timeout=15):
    sockets = (left, right)
    while True:
        readable, _, _ = select.select(sockets, [], [], idle_timeout)
        if not readable:
            return
        for source in readable:
            chunk = source.recv(16384)
            if not chunk:
                return
            target = right if source is left else left
            target.sendall(chunk)


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, upstream_port, allowed_network):
        self.upstream_port = upstream_port
        self.allowed_network = ipaddress.ip_network(allowed_network)
        self.capacity = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def verify_request(self, request, client_address):
        return ipaddress.ip_address(client_address[0]) in self.allowed_network

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()

    def handle_error(self, request, client_address):
        # Never dump network exceptions or request content into system logs.
        pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        try:
            with socket.create_connection(('127.0.0.1', self.server.upstream_port), timeout=3) as upstream:
                upstream.settimeout(15)
                forward(self.request, upstream)
        except OSError:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True, type=private_address)
    parser.add_argument('--port', type=int, default=18083)
    parser.add_argument('--upstream-port', type=int, default=18082)
    parser.add_argument('--allowed-network', required=True)
    args = parser.parse_args()
    if not all(1024 <= port <= 65535 for port in (args.port, args.upstream_port)):
        parser.error('Use unprivileged TCP ports')
    network = ipaddress.ip_network(args.allowed_network)
    if network.version != 4 or network.prefixlen < 16 or ipaddress.ip_address(args.bind) not in network:
        parser.error('Use the actual Docker bridge subnet, no broader than /16')
    with Relay((args.bind, args.port), args.upstream_port, str(network)) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
