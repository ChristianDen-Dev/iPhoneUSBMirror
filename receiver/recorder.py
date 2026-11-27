"""
recorder.py - records the mirror to an MP4 file (H.264 video + AAC sound).

- Video is re-encoded on the GPU (NVIDIA NVENC first; AMD / Intel / Windows
  Media Foundation / CPU x264 as fallbacks), already rotated upright, so the
  file plays correctly everywhere (TikTok, CapCut, Windows Photos).
- The frame size is fixed when recording starts. If the iPhone turns
  (portrait <-> landscape) the picture is fitted inside with black bars.
- Sound is the raw game audio from the iPhone (laptop volume/mute does not
  affect the recording).
- Runs in its own thread with a small queue: if the disk or encoder is slow,
  frames are dropped here, never in the live mirror.
"""

import os
import queue
import threading
import time
from fractions import Fraction

import av
import numpy as np

AUDIO_RATE = 48_000

# (encoder, options, pixel format) - tried in this order
ENCODERS = [
    ("h264_nvenc", {"preset": "p5", "tune": "hq", "rc": "vbr", "b": "20M", "maxrate": "35M",
                    "bufsize": "40M", "bf": "0", "g": "120"}, "nv12"),
    ("h264_amf", {"quality": "quality", "rc": "vbr_peak", "b": "20M", "maxrate": "35M", "bf": "0"}, "nv12"),
    ("h264_qsv", {"preset": "medium", "b": "20M", "maxrate": "35M"}, "nv12"),
    ("h264_mf", {"b": "20M", "hw_encoding": "1"}, "nv12"),
    ("libx264", {"preset": "veryfast", "crf": "18", "bf": "0", "g": "120"}, "yuv420p"),
]

ROTATE_FILTERS = {0: [], 1: [("transpose", "cclock")], 2: [("hflip", None), ("vflip", None)],
                  3: [("transpose", "clock")]}

_encoder_choice = None


def pick_encoder():
    """First encoder that really opens on this PC (cached)."""
    global _encoder_choice
    if _encoder_choice is None:
        for name, opts, pix in ENCODERS:
            try:
                cc = av.CodecContext.create(name, "w")
                cc.width, cc.height, cc.pix_fmt = 640, 360, pix
                cc.time_base = Fraction(1, 60)
                cc.framerate = Fraction(60, 1)
                cc.open()
                _encoder_choice = (name, opts, pix)
                break
            except Exception:
                continue
        if _encoder_choice is None:
            raise RuntimeError("no H.264 encoder available")
    return _encoder_choice


class Recorder:
    QUEUE_ITEMS = 90          # ~1.5 s of video; beyond that frames are dropped

    def __init__(self):
        self.active = False
        self.path = ""
        self.error = ""
        self.encoder_name = ""
        self.dropped = 0
        self.frames = 0
        self.started_at = 0.0
        self.on_finished = None   # callback(path, error) from the recorder thread
        self._q = queue.Queue(self.QUEUE_ITEMS)
        self._thread = None

    # ---- control (window thread) ------------------------------------------
    def start(self, path):
        if self.active:
            return
        self._q = queue.Queue(self.QUEUE_ITEMS)
        self.path, self.error, self.dropped, self.frames = path, "", 0, 0
        self.started_at = time.monotonic()
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="recorder")
        self._thread.start()

    def stop(self, wait_s=0.0):
        if not self.active:
            return
        self.active = False
        self._q.put(None)          # end marker (blocks only if the queue is full)
        if wait_s and self._thread:
            self._thread.join(wait_s)

    @property
    def elapsed(self):
        return time.monotonic() - self.started_at if self.active else 0.0

    @property
    def size_mb(self):
        try:
            return os.path.getsize(self.path) / 1e6
        except OSError:
            return 0.0

    # ---- data (video / network threads) ------------------------------------
    def video(self, frame, k, ts_us):
        if self.active:
            self._put(("v", frame, k, ts_us))

    def audio(self, ts_us, pcm):
        if self.active:
            self._put(("a", ts_us, pcm))

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    # ---- worker --------------------------------------------------------------
    def _run(self):
        out = vstream = astream = None
        graph = graph_key = None
        size = None
        t0_us = None
        last_pts = -1
        a_next = 0                 # next audio sample position in the file
        try:
            name, opts, pix = pick_encoder()
            self.encoder_name = name
            while True:
                item = self._q.get()
                if item is None:
                    break
                if item[0] == "v":
                    _, frame, k, ts = item
                    if out is None:
                        rw, rh = (frame.height, frame.width) if k % 2 else (frame.width, frame.height)
                        size = (rw & ~1, rh & ~1)
                        os.makedirs(os.path.dirname(self.path), exist_ok=True)
                        out = av.open(self.path, "w")
                        vstream = out.add_stream(name, rate=60, options=dict(opts))
                        vstream.width, vstream.height = size
                        vstream.pix_fmt = pix
                        vstream.codec_context.time_base = Fraction(1, 1000)
                        astream = out.add_stream("aac", rate=AUDIO_RATE, layout="stereo")
                        astream.bit_rate = 192_000
                        t0_us = ts
                        print(f"[rec] recording {size[0]}x{size[1]} with {name} -> {self.path}", flush=True)

                    key = (frame.width, frame.height, frame.format.name, k)
                    if key != graph_key:
                        graph, graph_key = self._make_graph(frame, k, size, pix), key
                    pts = max(last_pts + 1, (ts - t0_us) // 1000)      # ms, always increasing
                    last_pts = pts
                    frame.pts = pts
                    graph.push(frame)
                    f = graph.pull()
                    f.pts, f.time_base = pts, Fraction(1, 1000)
                    for p in vstream.encode(f):
                        out.mux(p)
                    self.frames += 1
                else:
                    _, ts, pcm = item
                    if out is None or not pcm:
                        continue                # audio starts with the first video frame
                    want = round((ts - t0_us) * AUDIO_RATE / 1e6)
                    if want < 0:
                        continue
                    if want > a_next + AUDIO_RATE // 10:               # gap > 100 ms: fill silence
                        a_next = self._audio_write(out, astream, b"\x00" * (min(want - a_next, AUDIO_RATE * 5) * 4),
                                                   a_next)
                    elif want < a_next - AUDIO_RATE // 10:             # far behind: drop this piece
                        continue
                    a_next = self._audio_write(out, astream, pcm, a_next)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            print(f"[rec] error: {self.error}", flush=True)
        finally:
            self.active = False
            if out is not None:
                try:
                    for p in vstream.encode():
                        out.mux(p)
                    for p in astream.encode():
                        out.mux(p)
                except Exception as e:
                    self.error = self.error or f"{type(e).__name__}: {e}"
                try:
                    out.close()
                except Exception as e:
                    self.error = self.error or f"{type(e).__name__}: {e}"
            elif not self.error:
                self.error = "no video arrived"
            print(f"[rec] finished: {self.frames} frames, {self.dropped} dropped"
                  f"{', ERROR ' + self.error if self.error else ''}", flush=True)
            if self.on_finished:
                try:
                    self.on_finished(self.path, self.error)
                except Exception:
                    pass

    @staticmethod
    def _audio_write(out, astream, pcm, pos):
        arr = np.frombuffer(pcm, dtype=np.int16).reshape(1, -1)
        af = av.AudioFrame.from_ndarray(arr, format="s16", layout="stereo")
        af.sample_rate = AUDIO_RATE
        af.pts, af.time_base = pos, Fraction(1, AUDIO_RATE)
        for p in astream.encode(af):
            out.mux(p)
        return pos + arr.shape[1] // 2

    @staticmethod
    def _make_graph(frame, k, size, pix):
        """rotate upright -> fit inside the fixed size (black bars) -> encoder format."""
        w, h = size
        g = av.filter.Graph()
        last = g.add_buffer(width=frame.width, height=frame.height, format=frame.format.name,
                            time_base=Fraction(1, 1000))
        chain = list(ROTATE_FILTERS[k])
        chain += [("scale", f"{w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bicubic"),
                  ("pad", f"{w}:{h}:(ow-iw)/2:(oh-ih)/2:black"),
                  ("format", pix)]
        for fname, arg in chain:
            node = g.add(fname, arg) if arg else g.add(fname)
            last.link_to(node)
            last = node
        sink = g.add("buffersink")
        last.link_to(sink)
        g.configure()
        return g
