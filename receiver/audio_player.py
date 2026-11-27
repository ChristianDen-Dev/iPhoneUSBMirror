"""
audio_player.py - plays the iPhone game sound on the default Windows output
device, so OBS / TikTok LIVE Studio "Desktop Audio" picks it up.

Input: raw PCM, 48 kHz, signed 16-bit little-endian, stereo interleaved
(AUDIO_PCM packets, see docs/PROTOCOL.md).

Low-latency jitter buffer:
  - plays through WASAPI (~20 ms) instead of the old MME path (~90 ms)
  - start playing once the target amount of audio is buffered; the target
    is ADAPTIVE: starts at 30 ms, grows 10 ms per dropout, shrinks slowly
  - SMOOTH DRIFT CORRECTION: when the buffer is more than DRIFT_BAND_MS away
    from the target, play ~1 % faster or slower (inaudible) until it is back.
    This replaces sudden jumps, which sounded like small clicks.
  - if the buffer runs dry -> play silence and re-buffer
  - if it still grows past MAX_MS (big hiccup) -> drop the oldest audio
Volume 0-150 % and mute keep the stream running, so un-muting is instant.
"""

import threading
import time

import numpy as np
import sounddevice as sd

RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = 4            # s16 * 2 channels
BLOCK_FRAMES = 240             # 5 ms per callback
TARGET_MS = 30                 # buffer we START with (small = low delay)
TARGET_MAX_MS = 120            # ADAPTIVE: each underrun adds 10 ms, up to this
TARGET_RELAX_S = 30            # ... and after this long without underruns it shrinks 5 ms
DRIFT_BAND_MS = 20             # outside target +/- this: speed up / slow down slightly
DRIFT_FRAMES = 3               # per 240-frame block = 1.25 % speed change
MAX_MS = 200                   # emergency: above this we skip ahead to TARGET_MS


def ms_to_bytes(ms):
    return int(RATE * ms / 1000) * BYTES_PER_FRAME


class AudioPlayer:
    def __init__(self):
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.started = False         # False = (re)buffering
        self.muted = False
        self.volume = 1.0            # 0.0 - 1.5
        self.stream = None
        self.underruns = 0
        self.skips = 0
        self.corrections = 0         # blocks played slightly faster/slower
        self.target_ms = TARGET_MS   # current (adaptive) buffer target
        self._last_underrun = time.monotonic()
        self.last_error = ""
        self.device_name = ""
        self._last_open_try = 0.0

    # ---- called from the network thread -----------------------------------
    def feed(self, ts_us, pcm: bytes):
        if self.stream is None:
            self._open()
        with self.lock:
            self.buf += pcm
            if len(self.buf) > ms_to_bytes(MAX_MS):
                del self.buf[: len(self.buf) - ms_to_bytes(self.target_ms)]
                self.skips += 1
            # adaptive buffer: shrink back slowly while playback is clean
            now = time.monotonic()
            if self.target_ms > TARGET_MS and now - self._last_underrun > TARGET_RELAX_S:
                self.target_ms = max(TARGET_MS, self.target_ms - 5)
                self._last_underrun = now

    def reset(self):
        """Call when the iPhone disconnects."""
        with self.lock:
            self.buf.clear()
            self.started = False

    # ---- called from the window -------------------------------------------
    def toggle_mute(self):
        self.muted = not self.muted
        return self.muted

    def set_volume(self, v):
        self.volume = min(1.5, max(0.0, float(v)))

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
            self.stream = self._open_wasapi() or sd.RawOutputStream(
                samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=BLOCK_FRAMES, latency="low", callback=self._callback)
            self.stream.start()
            if not self.device_name:
                self.device_name = sd.query_devices(kind="output")["name"] + " (MME)"
            print(f"[audio] playing on: {self.device_name}, output delay "
                  f"{self.stream.latency * 1000:.0f} ms + buffer {TARGET_MS}-{TARGET_MAX_MS} ms (adaptive)", flush=True)
        except Exception as e:
            self.stream = None
            self.last_error = str(e)
            print(f"[audio] cannot open output device: {e}", flush=True)

    def _open_wasapi(self):
        """WASAPI = the modern Windows sound path: ~20 ms instead of ~90 ms
        (old MME, the PortAudio default). Same default speakers/headset."""
        try:
            for api in sd.query_hostapis():
                if "WASAPI" in api["name"] and api["default_output_device"] >= 0:
                    dev = api["default_output_device"]
                    stream = sd.RawOutputStream(
                        device=dev, samplerate=RATE, channels=CHANNELS, dtype="int16",
                        blocksize=BLOCK_FRAMES, latency="low", callback=self._callback,
                        extra_settings=sd.WasapiSettings(auto_convert=True))   # any device rate
                    self.device_name = sd.query_devices(dev)["name"] + " (WASAPI)"
                    return stream
        except Exception as e:
            print(f"[audio] WASAPI not available ({e}) - using the default path", flush=True)
        return None

    def _callback(self, outdata, frames, time_info, status):
        need = frames * BYTES_PER_FRAME
        with self.lock:
            target = self.target_ms
            if not self.started:
                if len(self.buf) >= ms_to_bytes(target):
                    self.started = True
                else:
                    outdata[:] = b"\x00" * need
                    return
            have = len(self.buf)
            take_frames = frames
            if have > ms_to_bytes(target + DRIFT_BAND_MS):
                take_frames = frames + DRIFT_FRAMES          # play a little faster
            elif have < ms_to_bytes(target - DRIFT_BAND_MS):
                take_frames = frames - DRIFT_FRAMES          # play a little slower
            if have < take_frames * BYTES_PER_FRAME:
                take_frames = frames                         # not enough for "faster"
            take = take_frames * BYTES_PER_FRAME
            if have >= take:
                chunk = bytes(self.buf[:take])
                del self.buf[:take]
            else:
                # ran dry: play what we have + silence, then re-buffer
                chunk = bytes(self.buf) + b"\x00" * (need - have)
                take_frames = frames
                self.buf.clear()
                self.started = False
                self.underruns += 1
                self.target_ms = min(TARGET_MAX_MS, self.target_ms + 10)   # needs more cushion
                self._last_underrun = time.monotonic()
            if take_frames != frames:
                self.corrections += 1
        vol = 0.0 if self.muted else self.volume

        if take_frames == frames and vol == 1.0:
            outdata[:] = chunk                               # fast path: untouched
            return
        if vol == 0.0:
            outdata[:] = b"\x00" * need
            return
        a = np.frombuffer(chunk, dtype=np.int16).reshape(-1, CHANNELS).astype(np.float32)
        if take_frames != frames:
            # resample take_frames -> frames (linear; a 1 % change is inaudible)
            x = np.linspace(0, take_frames - 1, frames, dtype=np.float32)
            xp = np.arange(take_frames, dtype=np.float32)
            a = np.stack([np.interp(x, xp, a[:, 0]), np.interp(x, xp, a[:, 1])], axis=1)
        if vol != 1.0:
            a *= vol
        outdata[:] = np.clip(a, -32768, 32767).astype(np.int16).tobytes()
