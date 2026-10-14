"""Synthetic RTP/data-channel test from this Mac to deployed media via TURN.

No browser, microphone, model call or telephone call. Five-minute TURN credentials
travel only over the existing SSH session and are never logged or saved.
"""

import asyncio
import base64
import json
import shlex
from pathlib import Path
from uuid import uuid4

from aiortc import AudioStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parents[1]


def relay_only(sdp):
    lines = sdp.splitlines()
    candidates = [x for x in lines if x.startswith("a=candidate:") and " typ relay " in x]
    if not candidates:
        raise ValueError("No relay candidate gathered")
    return "\r\n".join(x for x in lines if not x.startswith("a=candidate:") or " typ relay " in x) + "\r\n"


REMOTE = r"""
import asyncio,json,sys
from uuid import uuid4
from aiortc import AudioStreamTrack,RTCConfiguration,RTCIceServer,RTCPeerConnection,RTCSessionDescription
from agent_platform.voice_ice import servers
async def main():
 ice=servers('synthetic-client-'+uuid4().hex)
 if not ice:raise ValueError('TURN is not configured')
 print(json.dumps({'ice':ice}),flush=True)
 server_ice=servers('synthetic-server-'+uuid4().hex)
 pc=RTCPeerConnection(RTCConfiguration([RTCIceServer(**s) for s in server_ice]))
 tasks=[]
 @pc.on('datachannel')
 def channel(dc):
  @dc.on('message')
  def message(value):
   if isinstance(value,str) and value.startswith('synthetic:'):dc.send(value)
 @pc.on('track')
 def track(t):
  async def receive():
   for _ in range(100):await t.recv()
  tasks.append(asyncio.create_task(receive()))
 pc.addTrack(AudioStreamTrack())
 try:
  offer=json.loads(await asyncio.to_thread(sys.stdin.readline))
  await pc.setRemoteDescription(RTCSessionDescription(offer['sdp'],'offer'))
  await pc.setLocalDescription(await pc.createAnswer())
  lines=pc.localDescription.sdp.splitlines()
  assert any(x.startswith('a=candidate:') and ' typ relay ' in x for x in lines)
  sdp='\r\n'.join(x for x in lines if not x.startswith('a=candidate:') or ' typ relay ' in x)+'\r\n'
  print(json.dumps({'sdp':sdp}),flush=True)
  await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline),30)
 finally:
  for t in tasks:t.cancel()
  await asyncio.gather(*tasks,return_exceptions=True)
  await pc.close()
asyncio.run(main())
"""


async def run():
    encoded = base64.b64encode(REMOTE.encode()).decode()
    command = shlex.join(
        [
            "docker",
            "exec",
            "-i",
            "agent-platform-media-1",
            "python",
            "-c",
            f"import base64;exec(base64.b64decode('{encoded}'))",
        ]
    )
    with (ROOT / ".runtime/voice/relay-verification.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "root@139.59.64.197",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
        )
        pc = None
        tasks = []
        try:

            async def read():
                raw = await asyncio.wait_for(process.stdout.readline(), 30)
                if not raw:
                    raise RuntimeError("Remote verification stopped; inspect private log")
                return json.loads(raw)

            ice = (await read())["ice"]
            pc = RTCPeerConnection(RTCConfiguration([RTCIceServer(**s) for s in ice]))
            heard = asyncio.Event()
            echoed = asyncio.Event()
            count = 0
            token = "synthetic:" + uuid4().hex
            dc = pc.createDataChannel("synthetic-network-verification")

            @dc.on("open")
            def opened():
                dc.send(token)

            @dc.on("message")
            def message(value):
                if value == token:
                    echoed.set()

            @pc.on("track")
            def track(incoming):
                async def receive():
                    nonlocal count
                    for _ in range(20):
                        await incoming.recv()
                        count += 1
                    heard.set()

                tasks.append(asyncio.create_task(receive()))

            pc.addTrack(AudioStreamTrack())
            await asyncio.wait_for(pc.setLocalDescription(await pc.createOffer()), 20)
            process.stdin.write((json.dumps({"sdp": relay_only(pc.localDescription.sdp)}) + "\n").encode())
            await process.stdin.drain()
            answer = await read()
            await pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], "answer"))
            await asyncio.wait_for(asyncio.gather(heard.wait(), echoed.wait()), 20)
            report = {
                "status": "PASS",
                "public_turn_relay": True,
                "rtp_audio_frames_received": count,
                "datachannel_roundtrip": True,
                "human_microphone_tested": False,
                "model_provider_requests": 0,
                "telephone_calls": 0,
            }
            (ROOT / "docs/verification/voice-public-relay.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if pc:
                await pc.close()
            if process.returncode is None:
                process.stdin.write(b"{}\n")
                try:
                    await process.stdin.drain()
                    await asyncio.wait_for(process.wait(), 5)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    process.terminate()
                    await process.wait()


if __name__ == "__main__":
    asyncio.run(run())
