"""Live AI SIP test with synthetic speech only; saves returned synthetic audio privately.

Requires the existing AI-mode configuration and consumes its model quota.
Set ASTERISK_HOST/PORT/SIP_SECRET_FILE as for the transport test.
"""

import argparse
import struct
import time
from pathlib import Path

import av
from asterisk_smoke import Sip


def encode_question(path):
    convert = av.AudioResampler(format="s16", layout="mono", rate=8000)
    encoder = av.CodecContext.create("pcm_mulaw", "w")
    encoder.sample_rate, encoder.layout, encoder.format = 8000, "mono", "s16"
    audio = bytearray()
    with av.open(str(path)) as source:
        for incoming in source.decode(audio=0):
            for frame in convert.resample(incoming):
                audio.extend(b"".join(bytes(p) for p in encoder.encode(frame)))
    for frame in convert.resample(None):
        audio.extend(b"".join(bytes(p) for p in encoder.encode(frame)))
    audio.extend(b"".join(bytes(p) for p in encoder.encode(None)))
    if not 1000 <= len(audio) <= 8000 * 10:
        raise ValueError("Use 0.125–10 seconds of synthetic speech")
    return bytes(audio)


def run(question, output):
    client = Sip()
    destination = client.open_call("700")
    phase, phase_started, last_voice = "greeting", time.monotonic(), None
    captured = bytearray()
    sent = 0
    start = time.monotonic()
    complete = False
    try:
        for sequence in range(5500):  # under the server's 120-second cap
            now = time.monotonic()
            payload = b"\xff" * 160
            if phase == "send":
                payload = question[sent : sent + 160].ljust(160, b"\xff")
                sent += 160
                if sent >= len(question):
                    phase, phase_started, last_voice = "reply", now, None
            client.rtp.sendto(
                struct.pack("!BBHII", 0x80, 0, sequence, sequence * 160, 12345) + payload, destination
            )
            while True:
                try:
                    data, _ = client.rtp.recvfrom(2048)
                except BlockingIOError:
                    break
                if len(data) < 12 or data[1] & 0x7F != 0:
                    continue
                offset = 12 + 4 * (data[0] & 15)
                if data[0] & 0x10:
                    offset += 4 + 4 * int.from_bytes(data[offset + 2 : offset + 4], "big")
                raw = data[offset:]
                if phase == "reply":
                    captured.extend(raw)
                if any(b not in {0xFF, 0x7F} for b in raw):
                    last_voice = now
            if phase in {"greeting", "reply"} and last_voice and now - last_voice > 1.2:
                if phase == "greeting":
                    print("PASS live AI greeting received", flush=True)
                    phase, phase_started, last_voice = "send", now, None
                else:
                    complete = True
                    break
            if now - phase_started > 50:
                raise TimeoutError("AI greeting or response was not received within 50 seconds")
            time.sleep(max(0, start + (sequence + 1) * 0.02 - time.monotonic()))
        if not complete:
            raise TimeoutError("Call deadline")
        client.hangup()
        output.parent.mkdir(parents=True, exist_ok=True)
        decoder = av.CodecContext.create("pcm_mulaw", "r")
        decoder.sample_rate, decoder.layout = 8000, "mono"
        with av.open(str(output), "w", format="wav") as result:
            stream = result.add_stream("pcm_s16le", rate=8000)
            stream.layout = "mono"
            for frame in decoder.decode(av.Packet(bytes(captured))):
                for packet in stream.encode(frame):
                    result.mux(packet)
            for packet in stream.encode(None):
                result.mux(packet)
        output.chmod(0o600)
        print("PASS live reply audio received and call ended; synthetic answer saved for verification")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("synthetic_question", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".runtime/asterisk/synthetic-answer.wav"))
    args = parser.parse_args()
    run(encode_question(args.synthetic_question), args.output)
