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
TARGET_RELAX_S = 8             # ... and after this long without underruns it shrinks 10 ms
DRIFT_BAND_MS = 20             # outside target +/- this: speed up / slow down slightly
DRIFT_FRAMES = 3               # per 240-frame block = 1.25 % speed change
MAX_MS = 200                   # emergency: above this we skip ahead to TARGET_MS
LATE_MS = 90                   # Wi-Fi burst: more than target + this waiting -> drop the oldest (it is late)
FAST_DRIFT_MS = 45             # more than target + this -> catch up 2x faster (2.5 %, not audible)


def ms_to_bytes(ms):
    return int(RATE * ms / 1000) * BYTES_PER_FRAME


STREAM_DEVICE = "CABLE Input"   # VB-Audio Virtual Cable: TikTok / OBS capture this, nobody hears it


def wasapi_outputs():
    """[(index, name)] of WASAPI playback devices + index of the Windows default."""
    for api in sd.query_hostapis():
        if "WASAPI" in api["name"]:
            devs = [(i, sd.query_devices(i)["name"]) for i in api["devices"]
                    if sd.query_devices(i)["max_output_channels"] > 0]
            return devs, api["default_output_device"]
    return [], -1


def find_output(match):
    devs, _ = wasapi_outputs()
    for i, name in devs:
        if match.lower() in name.lower() and "16ch" not in name:
            return i, name
    return None


class AudioPlayer:
    """Plays the game sound on ONE output device.
    device_match=None -> the laptop speakers (Windows default, never the cable)."""

    def __init__(self, device_match=None, label="laptop"):
        self.device_match = device_match
        self.label = label
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
        # second sound mixed on top (the iPhone mic, stream output only)
        self.mix = bytearray()
        self.mix_primed = False
        self.mix_gain = 1.0

    # ---- called from the network thread -----------------------------------
    def feed(self, ts_us, pcm: bytes):
        if self.stream is None:
            self._open()
        with self.lock:
            self.buf += pcm
            if len(self.buf) > ms_to_bytes(min(MAX_MS, self.target_ms + LATE_MS)):
                del self.buf[: len(self.buf) - ms_to_bytes(self.target_ms)]
                self.skips += 1
            # adaptive buffer: shrink back slowly while playback is clean
            now = time.monotonic()
            if self.target_ms > TARGET_MS and now - self._last_underrun > TARGET_RELAX_S:
                self.target_ms = max(TARGET_MS, self.target_ms - 10)
                self._last_underrun = now

    def feed_mix(self, pcm: bytes):
        """Sound to mix on top of the main sound (iPhone mic). Small buffer,
        no delay build-up: keeps at most 150 ms, then trims to the newest 60 ms."""
        if self.stream is None:
            self._open()
        with self.lock:
            self.mix += pcm
            if len(self.mix) > ms_to_bytes(150):
                del self.mix[: len(self.mix) - ms_to_bytes(60)]

    def reset_mix(self):
        with self.lock:
            self.mix.clear()
            self.mix_primed = False

    def reset(self):
        """Call when the iPhone disconnects."""
        with self.lock:
            self.buf.clear()
            self.mix.clear()
            self.mix_primed = False
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
            self.stream = self._open_wasapi()
            if self.stream is None and self.device_match:
                raise _NoDevice()
            self.stream = self.stream or sd.RawOutputStream(
                samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=BLOCK_FRAMES, latency="low", callback=self._callback)
            self.stream.start()
            if not self.device_name:
                self.device_name = sd.query_devices(kind="output")["name"] + " (MME)"
            print(f"[audio] {self.label}: {self.device_name}, output delay "
                  f"{self.stream.latency * 1000:.0f} ms + buffer {TARGET_MS}-{TARGET_MAX_MS} ms (adaptive)", flush=True)
        except _NoDevice:
            self.stream = None
            self.last_error = f"{self.device_match} not found"
        except Exception as e:
            self.stream = None
            self.last_error = str(e)
            print(f"[audio] {self.label}: cannot open output device: {e}", flush=True)

    def _pick_device(self):
        """WASAPI device index for this output, or None."""
        devs, default = wasapi_outputs()
        if self.device_match:
            hit = find_output(self.device_match)
            return hit[0] if hit else None
        names = dict(devs)
        if default >= 0 and "CABLE" not in names.get(default, ""):
            return default
        # Windows default is the virtual cable -> use the first real speaker instead
        for i, name in devs:
            if "CABLE" not in name and "VB-Audio" not in name:
                return i
        return None

    def _open_wasapi(self):
        """WASAPI = the modern Windows sound path: ~20 ms instead of ~90 ms
        (old MME, the PortAudio default)."""
        try:
            dev = self._pick_device()
            if dev is None:
                return None
            stream = sd.RawOutputStream(
                device=dev, samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=BLOCK_FRAMES, latency="low", callback=self._callback,
                extra_settings=sd.WasapiSettings(auto_convert=True))   # any device rate
            self.device_name = sd.query_devices(dev)["name"]
            return stream
        except Exception as e:
            print(f"[audio] {self.label}: WASAPI not available ({e})", flush=True)
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
            if have > ms_to_bytes(target + FAST_DRIFT_MS):
                take_frames = frames + 2 * DRIFT_FRAMES      # well behind: catch up faster
            elif have > ms_to_bytes(target + DRIFT_BAND_MS):
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
            mixchunk = None
            if self.mix:
                if not self.mix_primed and len(self.mix) >= ms_to_bytes(30):
                    self.mix_primed = True                   # small cushion before mixing
                if self.mix_primed:
                    if len(self.mix) >= need:
                        mixchunk = bytes(self.mix[:need])
                        del self.mix[:need]
                    else:
                        self.mix.clear()
                        self.mix_primed = False
        vol = 0.0 if self.muted else self.volume

        if take_frames == frames and vol == 1.0 and mixchunk is None:
            outdata[:] = chunk                               # fast path: untouched
            return
        if vol == 0.0 and mixchunk is None:
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
        if mixchunk is not None:
            a = a + np.frombuffer(mixchunk, dtype=np.int16).reshape(-1, CHANNELS).astype(np.float32) * self.mix_gain
        outdata[:] = np.clip(a, -32768, 32767).astype(np.int16).tobytes()


class _NoDevice(Exception):
    pass


def find_laptop_mic():
    """(index, name) of the real laptop/headset microphone (WASAPI), never the
    virtual cable. Prefers the Windows default recording device."""
    for api in sd.query_hostapis():
        if "WASAPI" not in api["name"]:
            continue
        ins = [(i, sd.query_devices(i)["name"]) for i in api["devices"]
               if sd.query_devices(i)["max_input_channels"] > 0]
        real = [(i, n) for i, n in ins if "CABLE" not in n and "VB-Audio" not in n]
        for i, n in real:
            if i == api["default_input_device"]:
                return i, n
        return real[0] if real else None
    return None


class LaptopMic:
    """Captures the laptop/headset microphone and hands each 5 ms piece
    (48 kHz s16 stereo) to on_pcm. WASAPI shared mode: ~10-20 ms delay."""

    def __init__(self, on_pcm):
        self.on_pcm = on_pcm
        self.stream = None
        self.device_name = ""
        self.error = ""

    def start(self):
        if self.stream is not None:
            return True
        dev = find_laptop_mic()
        if dev is None:
            self.error = "no microphone found"
            return False
        try:
            self.stream = sd.RawInputStream(
                device=dev[0], samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=BLOCK_FRAMES, latency="low",
                callback=lambda data, frames, t, status: self.on_pcm(bytes(data)),
                extra_settings=sd.WasapiSettings(auto_convert=True))
            self.stream.start()
            self.device_name, self.error = dev[1], ""
            print(f"[mic] laptop mic: {dev[1]}, delay {self.stream.latency * 1000:.0f} ms", flush=True)
            return True
        except Exception as e:
            self.stream, self.error = None, str(e)
            print(f"[mic] cannot open the laptop mic: {e}", flush=True)
            return False

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


class AudioRouter:
    """Sends the game sound to two places:
      STREAM : the VB-Audio virtual cable ("CABLE Input"). TikTok LIVE Studio /
               OBS capture it; nobody hears it -> the stream always has sound.
      LAPTOP : the laptop speakers / headset, with its own volume and mute.
    So muting the laptop never mutes the stream. Without the cable there is
    only the LAPTOP output (then muting it also mutes "Desktop Audio")."""

    def __init__(self, stream_enabled=True):
        self.laptop = AudioPlayer(None, "laptop")
        self.stream = AudioPlayer(STREAM_DEVICE, "stream")
        self.cable_name = (find_output(STREAM_DEVICE) or (None, ""))[1]
        self.stream_enabled = stream_enabled
        self.mic_source = "auto"   # stream mic: "auto" / "laptop" / "phone" / "off"
        # iPhone mic health (for Auto + diagnostics)
        self.phone_heard = 0.0     # last time the iPhone mic had real sound (not digital silence)
        self.phone_pieces = 0
        self.phone_silent_pieces = 0
        self.phone_peak = 0.0
        self.laptop_peak = 0.0
        self._auto_last = None
        self.mic_muted = False
        self.laptop_mic = LaptopMic(self._laptop_mic_pcm)
        self.mic_last = 0.0        # time the last mic piece arrived
        self.mic_level = 0.0
        self._mic_acc = 0.0
        self._mic_t = time.monotonic()
        self.level = 0.0          # loudest sample of the last ~0.1 s (0..1)
        self._acc = 0.0
        self._level_t = time.monotonic()

    @property
    def cable_ok(self):
        return bool(self.cable_name) and self.stream_enabled

    def feed(self, ts_us, pcm):
        self.laptop.feed(ts_us, pcm)
        if self.cable_ok:
            self.stream.feed(ts_us, pcm)
        a = np.frombuffer(pcm, dtype=np.int16)
        if a.size:
            self._acc = max(self._acc, float(np.abs(a).max()) / 32768.0)
        now = time.monotonic()
        if now - self._level_t >= 0.1:
            self.level, self._acc, self._level_t = self._acc, 0.0, now

    @property
    def mic_active(self):
        """True while the iPhone sends its microphone."""
        return time.monotonic() - self.mic_last < 1.5

    PHONE_SILENT = 8 / 32768       # below this the iPhone mic is muted by iOS (digital silence)
    PHONE_GRACE_S = 0.8            # Auto: how long the iPhone mic may be silent before switching

    @property
    def phone_mic_ok(self):
        """iPhone mic arrives AND has real sound (iOS gives exact silence when
        another app - e.g. Mobile Legends voice chat - takes the mic)."""
        return self.mic_active and time.monotonic() - self.phone_heard < self.PHONE_GRACE_S

    @property
    def effective_mic(self):
        """The mic that goes into the stream right now: "phone", "laptop" or "off"."""
        src = self.mic_source
        if src == "auto":
            src = "phone" if self.phone_mic_ok else "laptop"
            if src != self._auto_last:
                if self._auto_last is not None:
                    why = "iPhone mic is back" if src == "phone" else "iPhone mic silent / off"
                    print(f"[mic] {time.strftime('%H:%M:%S')} auto: now {src} mic ({why})", flush=True)
                self._auto_last = src
                self.stream.reset_mix()
        return src

    @property
    def mic_ready(self):
        """The chosen mic is really delivering sound."""
        eff = self.effective_mic
        if eff == "phone":
            return self.mic_active
        if eff == "laptop":
            return self.laptop_mic.stream is not None
        return False

    def mic_report(self):
        """One line for the log, then reset the counters."""
        n, sil = self.phone_pieces, self.phone_silent_pieces
        db = lambda v: f"{20 * np.log10(v):.0f} dB" if v > 0 else "-inf"
        text = (f"stream mic {self.mic_source}->{self.effective_mic}{' MUTED' if self.mic_muted else ''} | "
                f"iPhone mic {n} pieces, silent {100 * sil / n if n else 0:.0f}%, peak {db(self.phone_peak)} | "
                f"laptop mic peak {db(self.laptop_peak)} | game peak {db(self.level)}")
        self.phone_pieces = self.phone_silent_pieces = 0
        self.phone_peak = self.laptop_peak = 0.0
        return text

    def set_mic_source(self, source):
        """ONE mic at a time: laptop/headset, iPhone, or off (= TikTok/OBS's own mic)."""
        self.mic_source = source
        self.stream.reset_mix()
        self.mic_level = 0.0
        self._auto_last = None
        if source in ("laptop", "auto") and self.cable_ok:
            self.laptop_mic.start()
        else:
            self.laptop_mic.stop()

    def _mic_in(self, pcm):
        """Selected mic sound -> level meter + stream (never the laptop speakers)."""
        now = time.monotonic()
        a = np.frombuffer(pcm, dtype=np.int16)
        if a.size:
            self._mic_acc = max(self._mic_acc, float(np.abs(a).max()) / 32768.0)
        if now - self._mic_t >= 0.1:
            self.mic_level, self._mic_acc, self._mic_t = self._mic_acc, 0.0, now
        if self.cable_ok and not self.mic_muted:
            self.stream.feed_mix(pcm)

    def _laptop_mic_pcm(self, pcm):
        a = np.frombuffer(pcm, dtype=np.int16)
        if a.size:
            self.laptop_peak = max(self.laptop_peak, float(np.abs(a).max()) / 32768.0)
        if self.effective_mic == "laptop":
            self._mic_in(pcm)

    def feed_mic(self, ts_us, pcm):
        """iPhone mic pieces (only arrive when its Microphone toggle is on)."""
        now = time.monotonic()
        self.mic_last = now
        a = np.frombuffer(pcm, dtype=np.int16)
        peak = float(np.abs(a).max()) / 32768.0 if a.size else 0.0
        self.phone_pieces += 1
        self.phone_peak = max(self.phone_peak, peak)
        if peak > self.PHONE_SILENT:
            self.phone_heard = now
        else:
            self.phone_silent_pieces += 1
        if self.effective_mic == "phone":
            self._mic_in(pcm)

    def set_stream_enabled(self, on):
        self.stream_enabled = on
        if not on:
            self.stream.close()
            self.stream.reset()
            self.laptop_mic.stop()
        elif self.mic_source in ("laptop", "auto"):
            self.laptop_mic.start()

    def reset(self):
        self.laptop.reset()
        self.stream.reset()
        self.level = 0.0

    def close(self):
        self.laptop_mic.stop()
        self.laptop.close()
        self.stream.close()
