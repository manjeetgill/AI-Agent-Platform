"""Create local-only pilot secrets. Existing credentials are never overwritten."""

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".runtime/asterisk"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    for name, value in [
        ("sip-password", secrets.token_hex(24) + "\n"),
        ("bridge.json", json.dumps({"mode": "diagnostic"}, indent=2) + "\n"),
    ]:
        try:
            # Container secrets need to be readable by the unprivileged container user.
            # The enclosing host directory is owner-only; never put this file in Git.
            fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as out:
            out.write(value)
    print("Local Asterisk pilot configured in .runtime/asterisk (credentials not printed).")
    print("Mode: inspect bridge.json; new setups default to diagnostic tones, not an AI conversation.")
    print("Start: docker compose -f deploy/compose.asterisk.yaml up -d --build")


if __name__ == "__main__":
    main()
