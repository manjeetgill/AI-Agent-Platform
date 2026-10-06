"""Run the private, loopback-only Asterisk FAQ TLS gateway."""

import json
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_platform.asterisk_local_api import create_gateway

if __name__ == "__main__":
    state = ROOT / ".runtime/asterisk"
    settings = json.loads((state / "bridge.json").read_text())
    if settings.get("mode") != "agent":
        raise SystemExit("Configure agent mode first")
    uvicorn.run(
        create_gateway(settings),
        host="127.0.0.1",
        port=4063,
        access_log=False,
        ssl_keyfile=str(state / "server-key.pem"),
        ssl_certfile=str(state / "server-cert.pem"),
    )
