"""Bind the local SIP pilot to an existing local-console FAQ agent; secrets stay private."""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from local import load
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_platform.service_identity import provision


def save(path, value, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(value)


def certificates(state):
    files = ["platform-ca.pem", "server-cert.pem", "server-key.pem"]
    if all((state / name).is_file() for name in files):
        return
    if any((state / name).exists() for name in files):
        raise SystemExit("Partial TLS setup; inspect privately before replacing any certificate")
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local Asterisk pilot CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Asterisk local API")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("host.docker.internal"),
                    x509.DNSName("host.lima.internal"),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    # Trust is mounted only into this bridge; nothing is installed in macOS/browser trust stores.
    save(state / "platform-ca.pem", ca.public_bytes(serialization.Encoding.PEM), 0o644)
    save(state / "server-cert.pem", cert.public_bytes(serialization.Encoding.PEM), 0o644)
    save(
        state / "server-key.pem",
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        0o600,
    )


async def connect(name):
    state = ROOT / ".runtime/asterisk"
    if state.is_symlink():
        raise SystemExit("Refusing symlinked configuration")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    existing = json.loads((state / "bridge.json").read_text()) if (state / "bridge.json").exists() else {}
    if existing.get("mode") == "agent":
        raise SystemExit("Agent mode is already configured; existing credentials were preserved")
    values = load()
    gemini = values.get("AI_CALLING_AUDIO_API_KEY", "") or (
        values.get("AI_PLATFORM_FALLBACK_API_KEY", "")
        if values.get("AI_PLATFORM_FALLBACK_PROVIDER") == "gemini"
        else ""
    )
    if not gemini:
        raise SystemExit("Existing local Gemini key is missing")
    url = urlunsplit(urlsplit(values["AI_PLATFORM_DATABASE_URL"])._replace(path="/agent_console"))
    async with AsyncConnectionPool(url, open=False, kwargs={"row_factory": dict_row}) as pool:
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT a.* FROM web_voice_agents a
                JOIN platform_apps p ON p.id=a.app_id
                JOIN platform_external_users u ON u.app_id=p.id AND u.owner_id=a.owner_id
                WHERE p.tenant_id='local-console' AND p.name='Console' AND u.external_id='operator'
                AND a.name=%s AND a.enabled""",
                    (name,),
                )
            ).fetchall()
        if len(rows) != 1:
            raise SystemExit("Select exactly one enabled local-console agent by name")
        agent = rows[0]
        certificates(state)
        key = await provision(
            pool,
            tenant="local-console",
            organisation="Local operator",
            app_name="Console",
            app_id=agent["app_id"],
            scopes=["agents:run"],
            days=7,
        )
        try:
            save(
                state / "bridge.json",
                (
                    json.dumps(
                        {
                            "mode": "agent",
                            "platform_url": "https://host.docker.internal:4063",
                            "agent_id": str(agent["id"]),
                            "external_user_id": "operator",
                            "service_key": key["credential"],
                            "gemini_key": gemini,
                            "platform_ca": "/run/secrets/platform_ca",
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
                0o644,
            )
            save(
                state / "service-key-metadata.json",
                (
                    json.dumps(
                        {
                            "key_id": key["key_id"],
                            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                            "agent_name": name,
                            "agent_id": str(agent["id"]),
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
                0o600,
            )
        except BaseException:
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s", (key["key_id"],)
                )
            raise
    print("Connected local FAQ agent:", name)
    print("Dedicated agents:run credential saved privately; expires in 7 days. TLS verified by a private CA.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", required=True)
    asyncio.run(connect(parser.parse_args().agent_name))
