"""Bounded HTTPS/relay audio pilot using private, pre-budgeted test identities.

Only synthetic speech. Requires explicit operator approval of provider fees and
server-enforced spending limits before use. Does not certify human microphones.
"""

import asyncio
import fractions
import json
import os
import subprocess
import time
from array import array
from pathlib import Path
from uuid import uuid4

import av
import httpx
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from verify_voice_local import resample
from verify_voice_relay import relay_only

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://agents.myalgodesk.in/v1/"


async def run(index=0, *, hold_seconds=0, after_connected=None):
    from agent_platform import calling_audio, voice_config

    private = ROOT / ".runtime/voice/public-pilot.json"
    if private.stat().st_mode & 0o077:
        raise ValueError("Private identity permissions are unsafe")
    identities = json.loads(private.read_text())
    identity = identities[index]
    os.environ.update(json.loads((ROOT / ".runtime/voice/config.json").read_text()))
    assert voice_config.defaults().asr == voice_config.defaults().tts == "local"
    pcm, rate = await calling_audio.local_speak("What are the clinic opening hours?")
    outgoing = bytearray(resample(pcm, rate, 48000))
    sent = asyncio.Event()
    heard = asyncio.Event()
    remote = "docker exec agent-platform-media-1 python -c 'import json; from agent_platform.voice_ice import servers; print(json.dumps(servers(\"public-pilot\")))'"
    ice = json.loads(await asyncio.to_thread(subprocess.check_output, ["ssh", "root@139.59.64.197", remote]))
    pc = RTCPeerConnection(RTCConfiguration([RTCIceServer(**s) for s in ice]))
    received = bytearray()
    tasks = []
    session = None
    stats = {"frames": 0, "loud": 0.0, "input_end": None, "reply_start": None}

    class Speech(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.pts = 0
            self.started = None

        async def recv(self):
            if self.started is None:
                self.started = time.monotonic()
            await asyncio.sleep(max(0, self.started + self.pts / 48000 - time.monotonic()))
            data = bytes(outgoing[:1920]) if sent.is_set() else b""
            if sent.is_set():
                del outgoing[: len(data)]
                if not outgoing and stats["input_end"] is None:
                    stats["input_end"] = time.monotonic()
            frame = av.AudioFrame(format="s16", layout="mono", samples=960)
            frame.planes[0].update(data.ljust(1920, b"\0"))
            frame.sample_rate = 48000
            frame.pts = self.pts
            frame.time_base = fractions.Fraction(1, 48000)
            self.pts += 960
            return frame

    async def receive(track):
        converter = av.AudioResampler(format="s16", layout="mono", rate=16000)
        while True:
            frame = await track.recv()
            for f in converter.resample(frame):
                raw = bytes(f.planes[0])[: f.samples * 2]
                stats["frames"] += 1
                if any(abs(v) > 500 for v in array("h", raw)):
                    heard.set()
                    stats["loud"] = time.monotonic()
                    if stats["input_end"] and not stats["reply_start"]:
                        stats["reply_start"] = time.monotonic()
                if sent.is_set() and len(received) < 1920000:
                    received.extend(raw)

    @pc.on("track")
    def track(t):
        tasks.append(asyncio.create_task(receive(t)))

    pc.addTrack(Speech())
    started = time.monotonic()
    headers = {
        "Authorization": "Bearer " + identity["credential"],
        "X-External-User-ID": "public-audio-verifier",
    }
    async with httpx.AsyncClient(base_url=ORIGIN, headers=headers, timeout=65) as client:

        async def api(path, payload=None):
            r = await (client.get(path) if payload is None else client.post(path, json=payload))
            if r.status_code >= 400:
                raise RuntimeError(f"Public API {path}: HTTP {r.status_code}")
            return r.json()

        try:
            await api("voice/usage")
            from uuid import UUID

            app_id = str(UUID(identity["app_id"]))
            sql = f"SELECT currency,monthly FROM voice_spend_limits WHERE app_id='{app_id}'"
            import shlex

            command = (
                "docker exec agent-platform-db-1 psql -U postgres -d agent_platform -Atc " + shlex.quote(sql)
            )
            limit = (
                (
                    await asyncio.to_thread(
                        subprocess.check_output, ["ssh", "root@139.59.64.197", command], text=True
                    )
                )
                .strip()
                .split("|")
            )
            assert limit[0] == "INR" and 0 < float(limit[1]) <= 50
            tag = uuid4().hex
            doc = await api(
                "knowledge/documents",
                {
                    "external_id": "public-audio-" + tag,
                    "title": "Synthetic public voice fixture",
                    "text": "The test clinic opening hours are nine in the morning to five in the evening, Monday to Friday.",
                    "source_label": "Synthetic test data, not a real business",
                    "visibility": "app",
                },
            )
            agent = await api(
                "voice/agents",
                {
                    "external_id": "public-audio-" + tag,
                    "name": "Synthetic public audio verification",
                    "greeting": "Welcome to the voice test.",
                    "documents": [doc["id"]],
                    "language": "en",
                    "config": {**(await api("voice/capabilities"))["default_config"],
                               **identity.get("voice_config", {}), "barge_in": False},
                },
            )
            await pc.setLocalDescription(await pc.createOffer())
            result = await api(
                "voice/agents/" + agent["id"] + "/sessions",
                {
                    "sdp": relay_only(pc.localDescription.sdp),
                    "request_key": tag,
                    "audio_consent": True,
                    "transcript_consent": True,
                },
            )
            session = result["id"]
            await pc.setRemoteDescription(RTCSessionDescription(relay_only(result["sdp"]), "answer"))
            await asyncio.wait_for(heard.wait(), 30)
            if after_connected:
                await after_connected(agent["id"])
            while time.monotonic() - stats["loud"] < 1:
                await asyncio.sleep(0.2)
            sent.set()
            deadline = time.monotonic() + 65
            while time.monotonic() < deadline:
                events = (await api("voice/sessions/" + session + "/events"))["events"]
                if any(e["kind"] == "turn_failed" for e in events):
                    raise RuntimeError("Public voice turn failed; inspect session events")
                answers = [e for e in events if e["kind"] == "assistant"]
                if answers and any(
                    e["kind"] == "tts" and e["sequence"] > answers[-1]["sequence"] for e in events
                ):
                    await asyncio.sleep(2)
                    while time.monotonic() - stats["loud"] < 1:
                        await asyncio.sleep(0.2)
                    break
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError("No public reply within the deadline")
            answer = answers[-1]["content"]
            assert "nine" in answer.lower() or "9" in answer
            raw = bytes(received)
            voiced = [
                i for i in range(0, len(raw), 640) if any(abs(v) > 500 for v in array("h", raw[i : i + 640]))
            ]
            assert voiced
            transcript = await calling_audio.transcribe(raw[max(0, voiced[0] - 3200) : voiced[-1] + 3840])
            assert ("nine" in transcript.lower() or "9" in transcript) and (
                "five" in transcript.lower() or "5" in transcript
            )
            if hold_seconds:
                await asyncio.sleep(max(0, hold_seconds - (time.monotonic() - started)))
            await api("voice/sessions/" + session + "/end", {})
            other = {
                "Authorization": "Bearer " + identities[1 - index]["credential"],
                "X-External-User-ID": "public-audio-verifier",
            }
            isolation = await client.get("voice/sessions/" + session, headers=other)
            assert isolation.status_code == 404
            report = {
                "status": "PASS",
                "session_id": session,
                "app_id": identity["app_id"],
                "agent_id": agent["id"],
                "synthetic_audio": True,
                "human_microphone_tested": False,
                "public_https": True,
                "relay_only_client": True,
                "cross_app_read_denied": True,
                "reply": answer,
                "received_speech": transcript,
                "duration_seconds": round(time.monotonic() - started, 2),
                "usage": await api("voice/usage"),
            }
            (ROOT / f"docs/verification/voice-public-audio-{index}.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n"
            )
            print(json.dumps(report, default=str))
            return report
        except Exception as error:
            report = {
                "status": "FAIL",
                "session_id": session,
                "error_type": type(error).__name__,
                "reason": str(error)[:200],
                "no_automatic_retry": True,
            }
            (ROOT / f"docs/verification/voice-public-audio-{index}.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report))
            raise
        finally:
            if session:
                try:
                    await api("voice/sessions/" + session + "/end", {})
                except (httpx.HTTPError, RuntimeError):
                    print("Session end was not confirmed; inspect session state before retrying.")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pc.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--app-index", type=int, choices=[0, 1], default=0)
    asyncio.run(run(p.parse_args().app_index))
