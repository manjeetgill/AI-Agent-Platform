"""Two waves, two simultaneous synthetic callers. Bounded pilot, not a large-scale SLA."""

import asyncio
import json
import time
from uuid import uuid4

import httpx
from verify_voice_public import ORIGIN, ROOT, run


async def main():
    identities = json.loads((ROOT / ".runtime/voice/public-pilot.json").read_text())
    started = time.monotonic()
    reports = []
    overloads = []
    for wave in range(2):
        connected = [asyncio.Event(), asyncio.Event()]

        async def connect(index, agent, connected=connected):
            connected[index].set()
            await asyncio.wait_for(connected[1 - index].wait(), 30)
            if index == 0:
                async with httpx.AsyncClient(
                    base_url=ORIGIN,
                    timeout=15,
                    headers={
                        "Authorization": "Bearer " + identities[0]["credential"],
                        "X-External-User-ID": "public-audio-verifier",
                    },
                ) as c:
                    r = await c.post(
                        "voice/agents/" + agent + "/sessions",
                        json={
                            "sdp": "v=0\r\n" + "capacity probe " * 5,
                            "request_key": uuid4().hex,
                            "audio_consent": True,
                        },
                    )
                    overloads.append(r.status_code == 429)
                    assert r.status_code == 429, "Overload was not rejected"

        pair = await asyncio.gather(
            *(run(i, hold_seconds=85, after_connected=lambda a, i=i: connect(i, a)) for i in range(2)),
            return_exceptions=True,
        )
        reports.extend(
            [
                {k: v for k, v in p.items() if k not in {"usage", "reply", "received_speech"}}
                if isinstance(p, dict)
                else {"status": "FAIL", "error_type": type(p).__name__}
                for p in pair
            ]
        )
        if any(not isinstance(p, dict) for p in pair):
            break
        async with httpx.AsyncClient(timeout=10) as c:
            assert (await c.get("https://myalgodesk.in/login")).status_code == 200
    result = {
        "status": "PASS"
        if len(reports) == 4 and all(p["status"] == "PASS" for p in reports) and all(overloads)
        else "FAIL",
        "concurrent_sessions": 2,
        "waves_completed": len(reports) // 2,
        "waves_requested": 2,
        "duration_seconds": round(time.monotonic() - started, 2),
        "overload_rejected": all(overloads),
        "synthetic_audio": True,
        "human_microphone_verified": False,
        "scale_limit": "Only the configured two-call pilot tested; no higher capacity claim.",
        "runs": reports,
    }
    (ROOT / "docs/verification/voice-public-capacity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


asyncio.run(main())
