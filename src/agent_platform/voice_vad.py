"""Speech-aware voice activity detection for continuous mono 16-kHz PCM.

Energy alone mistakes fans/noise for speech. WebRTC VAD filters unvoiced
frames; the existing bounded endpointing and interruption logic remains.
"""
from array import array


class SpeechDetector:
    def __init__(self, threshold):
        import webrtcvad
        self.vad = webrtcvad.Vad(3)
        self.threshold = threshold
        self.pending = bytearray()
        self.voiced = False

    def feed(self, raw):
        self.pending.extend(raw)
        decisions = []
        while len(self.pending) >= 640:  # 20 ms, independent of incoming frame boundaries
            frame = bytes(self.pending[:640])
            del self.pending[:640]
            samples = array('h', frame)
            energy = sum(abs(x) for x in samples) / len(samples)
            decisions.append(energy > self.threshold and self.vad.is_speech(frame, 16000))
        if decisions:
            self.voiced = any(decisions)
        return self.voiced
