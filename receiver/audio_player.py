"""
audio_player.py - plays the iPhone game sound on the default Windows output
device, so OBS "Desktop Audio" picks it up.

Input: raw PCM, 48 kHz, signed 16-bit little-endian, stereo interleaved
(AUDIO_PCM packets, see docs/PROTOCOL.md).

Low-latency jitter buffer:
  - start playing once TARGET_MS of audio is buffered
  - if the buffer runs dry -> play silence and re-buffer
  - if the buffer grows past MAX_MS (clock drift / hiccup) -> drop the
    oldest audio back down to TARGET_MS, so delay never builds up
"""

import threading
import time

import sounddevice as sd

RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = 4            # s16 * 2 channels
BLOCK_FRAMES = 240             # 5 ms per callback
TARGET_MS = 80                 # buffer we aim for (jitter protection)
MAX_MS = 200                   # above this we skip ahead to TARGET_MS


def ms_to_bytes(ms):
    return int(RATE * ms / 1000) * BYTES_PER_FRAME


class AudioPlayer:
    def __init__(self):
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.started = False         # False = (re)buffering
        self.muted = False
        self.stream = None
        self.underruns = 0
        self.skips = 0
        self.last_error = ""
        self._last_open_try = 0.0

    # ---- called from the network thread -----------------------------------
    def feed(self, ts_us, pcm: bytes):
        if self.stream is None:
            self._open()
        with self.lock:
            if self.muted:
                self.buf.clear()
                return
            self.buf += pcm
            if len(self.buf) > ms_to_bytes(MAX_MS):
                del self.buf[: len(self.buf) - ms_to_bytes(TARGET_MS)]
                self.skips += 1

    def reset(self):
        """Call when the iPhone disconnects."""
        with self.lock:
            self.buf.clear()
            self.started = False

    def toggle_mute(self):
        with self.lock:
            self.muted = not self.muted
            self.buf.clear()
            self.started = False
        return self.muted

    @property
    def buffered_ms(self):
        with self.lock:
            return len(self.buf) / BYTES_PER_FRAME / RATE * 1000

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # ---- internals ---------------------------------------------------------
    def _open(self):
        now = time.monotonic()
        if now - self._last_open_try < 2.0:      # don't hammer a broken device
            return
        self._last_open_try = now
        try:
            self.stream = sd.RawOutputStream(
                samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=BLOCK_FRAMES, latency="low", callback=self._callback)
            self.stream.start()
            print(f"[audio] playing on: {sd.query_devices(kind='output')['name']}", flush=True)
        except Exception as e:
            self.stream = None
            self.last_error = str(e)
            print(f"[audio] cannot open output device: {e}", flush=True)

    def _callback(self, outdata, frames, time_info, status):
        need = frames * BYTES_PER_FRAME
        with self.lock:
            if not self.started:
                if len(self.buf) >= ms_to_bytes(TARGET_MS):
                    self.started = True
                else:
                    outdata[:] = b"\x00" * need
                    return
            if len(self.buf) >= need:
                outdata[:] = self.buf[:need]
                del self.buf[:need]
            else:
                # ran dry: play what we have + silence, then re-buffer
                have = len(self.buf)
                outdata[:have] = self.buf
                outdata[have:] = b"\x00" * (need - have)
                self.buf.clear()
                self.started = False
                self.underruns += 1
