"""Synthetic SIP/RTP pilot test: no microphone, model, carrier or real phone call.

Run inside the pilot Docker network (see docs/ASTERISK.md). Credentials are read
from a private file, never passed on the command line or printed.
"""

import hashlib
import os
import re
import socket
import struct
import time
from pathlib import Path
from uuid import uuid4


def md5(text):
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


class Sip:
    def __init__(self):
        self.host = os.environ.get("ASTERISK_HOST", "asterisk")
        self.port = int(os.environ.get("ASTERISK_PORT", "5060"))
        self.remote = (socket.gethostbyname(self.host), self.port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect(self.remote)
        self.ip, self.port_local = self.sock.getsockname()
        self.sock.settimeout(5)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind((self.ip, 0))
        self.rtp.setblocking(False)
        self.password = (
            Path(os.environ.get("SIP_SECRET_FILE", "/run/secrets/sip_password")).read_text().strip()
        )
        self.call_id, self.tag = uuid4().hex, uuid4().hex[:12]
        self.to = ""
        self.cseq = 0
        self.active = False

    def send(self, method, uri, body="", authorization="", cseq=None):
        self.cseq = cseq if cseq is not None else self.cseq + 1
        headers = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.ip}:{self.port_local};branch=z9hG4bK{uuid4().hex};rport",
            "Max-Forwards: 5",
            f"From: <sip:1001@{self.host}>;tag={self.tag}",
            self.to or f"To: <{uri}>",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} {method}",
            f"Contact: <sip:1001@{self.ip}:{self.port_local}>",
        ]
        if authorization:
            headers.append(authorization)
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append(f"Content-Length: {len(body.encode())}")
        self.sock.send(("\r\n".join(headers) + "\r\n\r\n" + body).encode())

    def response(self):
        while True:
            data = self.sock.recv(65535).decode()
            if data.startswith("SIP/2.0") and int(data.split()[1]) >= 200:
                return data

    def open_call(self, extension, expected=200):
        uri = self.uri = f"sip:{extension}@{self.host}"
        body = (
            f"v=0\r\no=pilot 1 1 IN IP4 {self.ip}\r\ns=synthetic-test\r\nc=IN IP4 {self.ip}\r\n"
            f"t=0 0\r\nm=audio {self.rtp.getsockname()[1]} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
        )
        self.send("INVITE", uri, body)
        challenge = self.response()
        assert challenge.startswith("SIP/2.0 401"), "Unauthenticated call was not challenged"
        self.send("ACK", uri, cseq=1)
        header = re.search(r"^WWW-Authenticate: (.+)", challenge, re.MULTILINE | re.IGNORECASE).group(1)
        fields = dict(re.findall(r'(\w+)="([^"]+)"', header))
        realm, nonce = fields["realm"], fields["nonce"]
        cnonce, nc = uuid4().hex, "00000001"
        a1, a2 = md5(f"1001:{realm}:{self.password}"), md5(f"INVITE:{uri}")
        if "qop" in fields:
            assert "auth" in fields["qop"].split(",")
            digest = md5(f"{a1}:{nonce}:{nc}:{cnonce}:auth:{a2}")
            extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
        else:
            digest, extra = md5(f"{a1}:{nonce}:{a2}"), ""
        auth = (
            f'Authorization: Digest username="1001", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{digest}", algorithm=MD5{extra}'
        )
        if "opaque" in fields:
            auth += f', opaque="{fields["opaque"]}"'
        self.send("INVITE", uri, body, auth, 2)
        response = self.response()
        assert response.startswith(f"SIP/2.0 {expected}"), (
            f"Unexpected SIP status: {response.splitlines()[0]}"
        )
        self.to = re.search(r"^To:.*", response, re.MULTILINE | re.IGNORECASE).group(0).strip()
        self.send("ACK", uri, cseq=2)
        if expected != 200:
            return
        self.active = True
        media_ip = re.search(r"c=IN IP4 (\S+)", response).group(1)
        media_port = int(re.search(r"m=audio (\d+)", response).group(1))
        return media_ip, media_port

    def hangup(self):
        self.active = False
        self.send("BYE", self.uri)
        assert self.response().startswith("SIP/2.0 200"), "Hangup was not acknowledged"

    def invite(self, extension, expected=200):
        destination = self.open_call(extension, expected)
        if expected != 200:
            return
        received, voiced_at = [], []
        started = time.monotonic()
        # Silence lets the bridge play its greeting tone; then a synthetic square
        # wave and silence trigger one VAD speech turn and a second diagnostic tone.
        for index in range(160):
            payload = bytes([0x9F, 0x1F]) * 80 if 45 <= index < 85 else b"\xff" * 160
            self.rtp.sendto(struct.pack("!BBHII", 0x80, 0, index, index * 160, 12345) + payload, destination)
            while True:
                try:
                    data, _ = self.rtp.recvfrom(2048)
                    received.append(data)
                    if any(b not in {0xFF, 0x7F} for b in data[12:]):
                        voiced_at.append(time.monotonic() - started)
                except BlockingIOError:
                    break
            time.sleep(max(0, started + (index + 1) * 0.02 - time.monotonic()))
        self.hangup()
        assert received and voiced_at, "No non-silent RTP returned"
        if extension == "700":
            assert min(voiced_at) < 1 and max(voiced_at) > 2, (
                "Expected greeting and response diagnostic tones"
            )
        print(f"PASS extension {extension}: authenticated SIP, bidirectional RTP, clean hangup")

    def close(self):
        if self.active:
            try:
                self.hangup()
            except (OSError, AssertionError):
                pass
        self.sock.close()
        self.rtp.close()


if __name__ == "__main__":
    for extension, expected in [("600", 200), ("700", 200), ("9876543210", 404)]:
        client = Sip()
        try:
            client.invite(extension, expected)
            if expected == 404:
                print("PASS external telephone number rejected; no trunk or carrier call attempted")
        finally:
            client.close()
