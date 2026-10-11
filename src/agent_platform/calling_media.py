"""WebRTC audio sessions for browser and WhatsApp transports. Optional voice dependencies keep text workers small."""

import asyncio
import fractions
import json
import os
import time
from contextlib import nullcontext
from uuid import uuid4

from . import calling_audio, voice_config, voice_ice, voice_usage


def available():
    try:
        import aiortc  # noqa: F401
        import av  # noqa: F401
        import webrtcvad  # noqa: F401

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
        self.turn_task = None
        self.partial_task = None
        self.utterance = 0
        self.generation = 0
        self.profile = voice_config.current().model_dump()
        self.event = None
        self.meter = None
        self.supports_streaming = False
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
                await self.emit("connected")
                self.turn_task = self.spawn(self.welcome())
            elif self.pc.connectionState == "disconnected":
                self.spawn(self.disconnect_grace())
            elif self.pc.connectionState == "failed":
                await self.fail('connection')

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() and not self.closed:
                self.spawn(self.fail('media_task'))

        task.add_done_callback(finished)
        return task

    async def emit(self, kind, details=None, text=None):
        if self.event:
            await self.event(kind, details, text)

    async def fail(self, stage):
        if self.closed:
            return
        try:
            await self.emit('failed', {'stage': stage, 'outcome': 'failed'})
        finally:
            await self.close()

    async def welcome(self):
        generation = self.generation
        self.busy = True
        try:
            with voice_config.using(self.profile), (voice_usage.using(*self.meter) if self.meter else nullcontext()):
                await asyncio.wait_for(self.say("Hello. This is an AI assistant. " + self.greeting, generation), 45)
        finally:
            if generation == self.generation:
                self.busy = False

    async def disconnect_grace(self):
        await asyncio.sleep(5)
        if not self.closed and self.pc.connectionState != 'connected':
            await self.fail('network_disconnect')

    async def deadline(self):
        await asyncio.sleep(120)
        await self.close()

    async def interrupt(self):
        self.generation += 1
        self.utterance += 1
        if self.partial_task:
            self.partial_task.cancel()
        self.audio.clear()
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
            # Do not wait on uncooperative upstream cancellation in the audio reader.
        self.busy = False
        await self.emit('interrupted', {'outcome': 'playback_cleared'})

    async def partial(self, pcm, utterance):
        # Local-only rolling recognition. Never send speculative duplicates to paid ASR.
        try:
            with voice_config.using(self.profile), (voice_usage.using(*self.meter) if self.meter else nullcontext()):
                text = await asyncio.wait_for(calling_audio.transcribe(pcm), 10)
            if text.strip() and utterance == self.utterance and not self.closed:
                await self.emit('asr_partial', {'provider': 'local', 'outcome': 'provisional'}, text)
        except Exception:  # noqa: BLE001 -- provisional failure must not expose upstream diagnostics
            await self.emit('asr_partial', {'provider': 'local', 'outcome': 'unavailable'})

    async def say(self, text, generation=None):
        import av

        generation = self.generation if generation is None else generation
        started = time.monotonic()
        seconds = 0
        async for pcm, rate in calling_audio.speech_chunks(text):
            if self.closed or generation != self.generation:
                return
            if rate not in {8000, 16000, 22050, 24000, 44100, 48000} or not pcm or len(pcm) % 2:
                raise ValueError("Unsupported speech response")
            seconds += len(pcm) / (rate * 2)
            if seconds > 40:
                raise ValueError("Speech response too long")
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
            frame.planes[0].update(pcm)
            frame.sample_rate = rate
            convert = av.AudioResampler(format="s16", layout="mono", rate=48000)
            for f in convert.resample(frame) + convert.resample(None):
                if len(self.audio) + f.samples * 2 > 48000 * 2 * 40:
                    raise ValueError('Voice output buffer exceeds limit')
                self.audio.extend(bytes(f.planes[0])[: f.samples * 2])
        await self.emit('tts', {'duration_ms': int((time.monotonic()-started)*1000),
                              'provider': calling_audio.speech_provider('tts'), 'characters': len(text)})

    async def consume(self, track):
        from aiortc.mediastreams import MediaStreamError
        from av import AudioResampler

        from .voice_vad import SpeechDetector

        resample = AudioResampler(format="s16", layout="mono", rate=16000)
        chunks, quiet, onset = bytearray(), 0, bytearray()
        preroll = bytearray()
        partial_at = 32000
        config = voice_config.VoiceConfig.model_validate(self.profile)
        detector = SpeechDetector(config.speech_threshold)
        try:
            while not self.closed:
                incoming = await track.recv()
                for frame in resample.resample(incoming):
                    raw = bytes(frame.planes[0])[: frame.samples * 2]
                    loud = detector.feed(raw)
                    preroll.extend(raw)
                    del preroll[:-32000]  # preserve the first words before the VAD trigger
                    if self.busy or self.audio:
                        if config.barge_in and loud:
                            onset.extend(raw)
                            if len(onset) < 6400:  # 200 ms detected speech, not a single noisy frame
                                continue
                            await self.interrupt()
                            chunks = bytearray(preroll)
                            onset.clear()
                        else:
                            onset.clear()
                            continue
                    elif loud or chunks:
                        chunks.extend(raw)
                    quiet = 0 if loud else quiet + len(raw)
                    if config.partial_transcripts and config.asr == 'local' and len(chunks) >= partial_at and partial_at <= 352000 and (not self.partial_task or self.partial_task.done()):
                        self.partial_task = self.spawn(self.partial(bytes(chunks), self.utterance))
                        partial_at += 32000
                    if len(chunks) >= 384000 or (chunks and quiet >= config.silence_ms * 32):
                        self.utterance += 1
                        if self.partial_task:
                            self.partial_task.cancel()
                        self.busy = True
                        self.turn_task = self.spawn(self.speech_turn(bytes(chunks[:384000])))
                        chunks.clear()
                        quiet = 0
                        partial_at = 32000
        except MediaStreamError:
            # Browser End call stops its track before the HTTP end request arrives.
            # A normal end-of-stream is not a media decoder failure. ICE failure is
            # reported separately by connectionstatechange.
            await self.close()
        except Exception:  # noqa: BLE001 -- no audio/transcripts in exceptions or logs
            await self.fail('media_receive')

    async def speech_turn(self, pcm):
        generation = self.generation
        try:
            with voice_config.using(self.profile) as config, (voice_usage.using(*self.meter) if self.meter else nullcontext()):
                await asyncio.wait_for(self.process_turn(pcm, generation), config.turn_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- report only stage, never provider diagnostics
            await self.emit('turn_failed', {'outcome': 'failed', 'stage': 'audio_turn'})
            await self.close()
        finally:
            if generation == self.generation:
                self.busy = False

    async def process_turn(self, pcm, generation):
        started = time.monotonic()
        text = await calling_audio.transcribe(pcm)
        await self.emit('asr', {'duration_ms': int((time.monotonic()-started)*1000),
                              'provider': calling_audio.speech_provider()})
        if not text.strip() or generation != self.generation or self.closed:
            return
        await self.emit('user', {'characters': len(text)}, text)
        started = time.monotonic()
        streamed = []

        async def on_claim(quotation):
            if generation != self.generation or self.closed:
                raise asyncio.CancelledError()
            # Never persist speculative raw tokens. This callback receives a validated quotation.
            await self.say(quotation, generation)
            streamed.append(quotation)
            await self.emit('streamed_claim', {'characters': len(quotation), 'outcome': 'source_validated'})

        if self.supports_streaming and voice_config.current().stream_grounded_quotes:
            reply = await self.respond(text, on_claim)
        else:
            reply = await self.respond(text)
        await self.emit('reasoning', {'duration_ms': int((time.monotonic()-started)*1000),
                                    'provider': voice_config.current().reasoning,
                                    'model': voice_config.current().model, 'outcome': reply['action']})
        if generation != self.generation or self.closed:
            return
        await self.emit('assistant', {'characters': len(reply['text'])}, reply['text'])
        if not streamed:
            await self.say(reply["text"], generation)
        elif reply['action'] != 'continue':
            # An incomplete stream must not keep emitting a newly generated answer.
            await self.emit('turn_failed', {'stage': 'stream_completion', 'outcome': 'failed'})
            await self.close()
            return
        if reply["action"] != "continue" and generation == self.generation:
            while self.audio and not self.closed and generation == self.generation:
                await asyncio.sleep(0.1)
            if generation == self.generation:
                await self.close()

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
