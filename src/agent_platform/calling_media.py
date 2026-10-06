"""WebRTC audio sessions for browser and WhatsApp transports. Optional voice dependencies keep text workers small."""

import asyncio
import fractions
import json
import os
import time
from uuid import uuid4

from . import calling_audio, voice_ice


def available():
    try:
        import aiortc  # noqa: F401
        import av  # noqa: F401

        calling_audio.key()
        calling_audio.speech_key()
        return os.environ.get("AI_WHATSAPP_CALLING_ENABLED") == "1"
    except (ImportError, ValueError):
        return False


class Session:
    def __init__(self, respond, greeting, ended):
        from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection
        from av import AudioFrame

        servers = [
            RTCIceServer(**s)
            for s in (
                voice_ice.servers("server-" + uuid4().hex)
                or json.loads(
                    os.environ.get("AI_VOICE_ICE_SERVERS", os.environ.get("AI_CALLING_ICE_SERVERS", "[]"))
                )
            )
        ]
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self.respond, self.greeting, self.ended = respond, greeting, ended
        self.tasks, self.closed = set(), False
        self.audio = bytearray()
        self.busy = False
        self.timer = self.spawn(self.deadline())
        self.greeted = False
        owner = self

        class Output(MediaStreamTrack):
            kind = "audio"

            def __init__(self):
                super().__init__()
                self.pts, self.started = 0, None

            async def recv(self):
                if self.started is None:
                    self.started = time.monotonic()
                await asyncio.sleep(max(0, self.started + self.pts / 48000 - time.monotonic()))
                data = bytes(owner.audio[:1920])
                del owner.audio[: len(data)]
                frame = AudioFrame(format="s16", layout="mono", samples=960)
                frame.planes[0].update(data.ljust(1920, b"\0"))
                frame.sample_rate, frame.pts, frame.time_base = 48000, self.pts, fractions.Fraction(1, 48000)
                self.pts += 960
                return frame

        self.pc.addTrack(Output())

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self.spawn(self.consume(track))

        @self.pc.on("connectionstatechange")
        async def state_changed():
            if self.pc.connectionState == "connected" and not self.greeted:
                self.greeted = True
                self.spawn(self.welcome())
            elif self.pc.connectionState == "failed":
                await self.close()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() and not self.closed:
                self.spawn(self.close())

        task.add_done_callback(finished)
        return task

    async def welcome(self):
        self.busy = True
        try:
            await self.say("Hello. This is an AI assistant. " + self.greeting)
        finally:
            self.busy = False

    async def deadline(self):
        await asyncio.sleep(120)
        await self.close()

    async def say(self, text):
        import av

        pcm, rate = await calling_audio.speak(text)
        if len(pcm) > rate * 2 * 40:
            raise ValueError("Speech response too long")
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
        frame.planes[0].update(pcm)
        frame.sample_rate = rate
        convert = av.AudioResampler(format="s16", layout="mono", rate=48000)
        for f in convert.resample(frame) + convert.resample(None):
            self.audio.extend(bytes(f.planes[0])[: f.samples * 2])

    async def consume(self, track):
        from array import array

        from av import AudioResampler

        resample = AudioResampler(format="s16", layout="mono", rate=16000)
        chunks, quiet = bytearray(), 0
        try:
            while not self.closed:
                incoming = await track.recv()
                for frame in resample.resample(incoming):
                    raw = bytes(frame.planes[0])[: frame.samples * 2]
                    if self.busy or self.audio:
                        chunks.clear()
                        quiet = 0
                        continue
                    samples = array("h", raw)
                    loud = bool(samples) and sum(abs(x) for x in samples) / len(samples) > 300
                    if loud or chunks:
                        chunks.extend(raw)
                        quiet = 0 if loud else quiet + len(raw)
                    if len(chunks) >= 384000 or (chunks and quiet >= 25600):
                        self.busy = True
                        self.spawn(self.speech_turn(bytes(chunks)))
                        chunks.clear()
                        quiet = 0
        except Exception:  # noqa: BLE001 -- no audio/transcripts in exceptions or logs
            await self.close()

    async def speech_turn(self, pcm):
        try:
            text = await calling_audio.transcribe(pcm)
            if text.strip():
                reply = await self.respond(text)
                await self.say(reply["text"])
                if reply["action"] != "continue":
                    while self.audio and not self.closed:
                        await asyncio.sleep(0.1)
                    await self.close()
        finally:
            self.busy = False

    async def offer(self):
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return self.pc.localDescription.sdp

    async def remote(self, sdp, kind):
        from aiortc import RTCSessionDescription

        if kind not in {"offer", "answer"} or len(sdp) > 65536:
            raise ValueError("Invalid media offer")
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
        if kind == "offer":
            await self.pc.setLocalDescription(await self.pc.createAnswer())
            return self.pc.localDescription.sdp

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in tuple(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        self.audio.clear()
        await self.pc.close()
        await self.ended()
