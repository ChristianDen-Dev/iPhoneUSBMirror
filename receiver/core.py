"""
core.py - the "engine" of the receiver (no window code here):

  Receiver thread : connects to the iPhone over USB (usbmux, port 2345),
                    reads packets, plays audio at once, hands video on.
  VideoWorker     : decodes H.264 on the GPU (CPU fallback), always shows
                    the NEWEST frame (never builds a delay), feeds the
                    recorder and takes screenshots.
  Shared          : state shared with the window thread.

Protocol: see docs/PROTOCOL.md
"""

import asyncio
import collections
import os
import re
import socket
import struct
import threading
import time
from fractions import Fraction

import av
from av.codec.hwaccel import HWAccel
import numpy as np
from pymobiledevice3 import usbmux

DEVICE_PORT = 2345
AUDIO_PORT = 2346               # sound-only channel (newer iPhone app); falls back to the main one
SOCKET_TIMEOUT_S = 3.0          # iPhone sends a heartbeat every 1 s
MAX_PAYLOAD = 8 * 1024 * 1024   # sanity limit for one packet

HEADER = struct.Struct(">BBQI")  # type, flags, timestamp_us, length
T_VIDEO_CONFIG, T_VIDEO_FRAME, T_AUDIO_PCM, T_ORIENTATION, T_HEARTBEAT, T_INFO, T_AUDIO_MIC = 1, 2, 3, 4, 5, 6, 7
FLAG_KEYFRAME = 0x01

# CGImagePropertyOrientation -> number of 90 deg counter-clockwise turns
#   1 up: 0 | 3 down: 180 | 6: 90 CCW | 8: 90 CW (= 3 CCW)
# (6/8 directions confirmed on a real iPhone 14 Pro Max with Mobile Legends)
ROTATION_K = {1: 0, 2: 0, 3: 2, 4: 2, 5: 3, 6: 1, 7: 1, 8: 3}

# np.rot90-style k (CCW quarter turns) -> FFmpeg filters
ROTATE_FILTERS = {0: [], 1: [("transpose", "cclock")], 2: [("hflip", None), ("vflip", None)],
                  3: [("transpose", "clock")]}

H264_PROFILES = {66: "Baseline", 77: "Main", 88: "Extended", 100: "High"}

# Connection states (shown in the window)
ST_NO_USB, ST_NO_APP, ST_CONNECTED = "no_usb", "no_app", "connected"


def describe_sps(annexb: bytes) -> str:
    """'H.264 High' etc. from the SPS inside an Annex-B parameter-set blob."""
    i = annexb.find(b"\x00\x00\x01")
    while i != -1 and i + 4 < len(annexb):
        nal = annexb[i + 3]
        if nal & 0x1F == 7:                     # SPS: profile_idc follows the NAL header
            prof = annexb[i + 4]
            return f"H.264 {H264_PROFILES.get(prof, prof)}"
        i = annexb.find(b"\x00\x00\x01", i + 3)
    return "H.264"


class NoDeviceError(Exception):
    pass


# --------------------------------------------------------------------------
class Shared:
    """State shared between the network/video threads and the window."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = True
        self.state = ST_NO_USB
        self.frame = None            # newest BGRA numpy array, already window-sized
        self.frame_seq = 0
        self.frame_ready = threading.Event()   # wakes the window loop on a new frame
        self.target_size = (1280, 720)          # video area size (set by the window)
        self.orientation = 1
        self.flip_landscape = False
        self.sharpen = True
        # filled by the engine
        self.video_size = (0, 0)     # upright size of the iPhone picture
        self.codec = ""              # "H.264 High"
        self.decoder_name = ""
        self.info = ""               # last diagnostic text from the iPhone
        self.sync_text = ""          # last audio timing report (SyncMeter)
        self.audio_channel = "main"  # "separate" when the sound-only channel is connected
        self.bytes_total = 0
        self.frames_in = 0           # decoded frames (= what the iPhone sends)
        self.frames_decoded = 0      # frames prepared for the window
        self.frames_skipped = 0      # decoded but not drawn (a newer one was waiting)
        self.decode_ms = 0.0
        self.render_ms = 0.0
        self.window_ms = 0.0
        self.connected_since = 0.0
        self.last_video_ts = 0
        self.last_audio_ts = 0
        # helpers set by the window
        self.audio = None            # AudioPlayer
        self.recorder = None         # Recorder
        self.snapshot_request = None  # (path, callback) -> handled by the video thread

    def rotation_k(self):
        k = ROTATION_K.get(self.orientation, 0)
        if self.flip_landscape and k in (1, 3):
            k = (k + 2) % 4
        return k

    @property
    def thermal(self):
        """iPhone heat level from the INFO text: nominal / fair / serious / critical."""
        m = re.search(r"thermal (\w+)", self.info or "")
        return m.group(1).lower() if m else ""

    def clear_frame(self):
        with self.lock:
            self.frame = None
            self.frame_seq += 1
        self.frame_ready.set()


# --------------------------------------------------------------------------
class VideoWorker(threading.Thread):
    """Decode every packet (H.264 needs them all) but only draw the NEWEST
    frame when behind -> always low delay, never a backlog."""
    MAX_BACKLOG = 90            # packets (~1.5 s); beyond this: drop all, wait for keyframe
    MIN_DRAW_INTERVAL_S = 0.1   # even when behind, draw at least 10x per second
    HW_DEVICES = ("cuda", "d3d11va")   # NVIDIA, then any GPU; CPU last

    def __init__(self, receiver):
        super().__init__(daemon=True, name="video")
        self.rx = receiver
        self.s = receiver.s
        self.cv = threading.Condition()
        self.q = collections.deque()
        self._reset = True

    def reset(self):
        """New connection: drop queued packets, start a fresh decoder."""
        with self.cv:
            self.q.clear()
            self._reset = True

    def submit(self, data, is_key, ts_us):
        with self.cv:
            if len(self.q) >= self.MAX_BACKLOG:
                print("[mirror] video backlog too big - skipping ahead to next keyframe", flush=True)
                self.q.clear()
                self._reset = True
            self.q.append((data, is_key, ts_us))
            self.cv.notify()

    def run(self):
        decoder = None
        need_keyframe = True
        last_draw = 0.0
        while self.s.running:
            with self.cv:
                while not self.q and self.s.running:
                    self.cv.wait(0.5)
                if not self.s.running:
                    break
                if self._reset:
                    self._reset = False
                    decoder = self.make_decoder()
                    need_keyframe = True
                data, is_key, ts_us = self.q.popleft()

            if need_keyframe and not is_key:
                continue                            # wait for a clean start
            t0 = time.perf_counter()
            try:
                frames = decoder.decode(av.Packet(data))
            except av.FFmpegError as e:
                print(f"[mirror] decode error: {e} - waiting for next keyframe", flush=True)
                need_keyframe = True
                continue
            t1 = time.perf_counter()
            need_keyframe = False
            self.s.last_video_ts = ts_us
            self.s.decode_ms = (t1 - t0) * 1000
            if not frames:
                continue
            self.s.frames_in += len(frames)
            k = self.s.rotation_k()

            rec = self.s.recorder
            if rec is not None and rec.active:
                for f in frames:
                    rec.video(f, k, ts_us)

            with self.cv:
                behind = len(self.q)
            if behind == 0 or t1 - last_draw >= self.MIN_DRAW_INTERVAL_S:
                self.rx.present(frames[-1], k)
                last_draw = t1
                self.s.render_ms = (time.perf_counter() - t1) * 1000
            else:
                self.s.frames_skipped += 1          # a newer frame is already waiting

            req = self.s.snapshot_request
            if req is not None:
                self.s.snapshot_request = None
                self.rx.snapshot(frames[-1], k, *req)

    def make_decoder(self):
        for dev in self.HW_DEVICES:
            try:
                ctx = av.CodecContext.create("h264", "r", hwaccel=HWAccel(device_type=dev,
                                                                            allow_software_fallback=False))
                if self.s.decoder_name != dev:
                    print(f"[mirror] video decoder: GPU ({dev})", flush=True)
                self.s.decoder_name = dev
                return ctx
            except Exception as e:
                print(f"[mirror] GPU decoder {dev} not available: {e}", flush=True)
        ctx = av.CodecContext.create("h264", "r")
        ctx.thread_type = "SLICE"                   # slice threads add no delay
        if self.s.decoder_name != "cpu":
            print("[mirror] video decoder: CPU", flush=True)
        self.s.decoder_name = "cpu"
        return ctx


# --------------------------------------------------------------------------
class FrameRenderer:
    """One FFmpeg filter graph does everything in C (fast):
    rotate (in YUV) -> scale only if needed -> optional sharpen -> output format."""
    UPSCALE_SHARPEN_AT = 1.15
    UNSHARP = "5:5:1.0:5:5:0.0"   # luma 5x5 amount 1.0, chroma off

    def __init__(self, out_format="bgra"):
        self.out_format = out_format
        self._graph = None
        self._key = None
        self._pts = 0

    def render(self, frame, k, out_w, out_h, sharpen=False):
        rw, rh = (frame.height, frame.width) if k % 2 else (frame.width, frame.height)
        upscale = out_w / rw
        sharpen = sharpen and upscale > self.UPSCALE_SHARPEN_AT
        key = (frame.width, frame.height, frame.format.name, k, out_w, out_h, sharpen)
        if key != self._key:
            g = av.filter.Graph()
            last = g.add_buffer(width=frame.width, height=frame.height,
                                format=frame.format.name, time_base=Fraction(1, 1000))
            chain = list(ROTATE_FILTERS[k])
            if (out_w, out_h) != (rw, rh):   # 1:1 (Stream Mode) skips scaling entirely
                flags = "lanczos" if upscale > 1.0 else "bilinear"
                chain.append(("scale", f"{out_w}:{out_h}:flags={flags}"))
            if sharpen:
                chain.append(("unsharp", self.UNSHARP))
            chain.append(("format", self.out_format))
            for name, arg in chain:
                node = g.add(name, arg) if arg else g.add(name)
                last.link_to(node)
                last = node
            sink = g.add("buffersink")
            last.link_to(sink)
            g.configure()
            self._graph, self._key = g, key
        self._pts += 1
        frame.pts = self._pts
        self._graph.push(frame)
        return np.ascontiguousarray(self._graph.pull().to_ndarray())


# --------------------------------------------------------------------------
class SyncMeter:
    """Measures the sound path every 5 s (for tuning the audio delay):
    - size of the sound pieces the iPhone sends and gaps between them
    - how much LATER sound arrives than video, compared with the moment the
      iPhone captured each one (same iPhone clock for both)."""
    PERIOD_S = 5.0

    def __init__(self):
        self.t0 = time.perf_counter()
        self._reset()

    def _reset(self):
        self.chunks, self.chunk_ms_max, self.chunk_ms_sum = 0, 0.0, 0.0
        self.gap_max = 0.0
        self.last_audio_arrival = None
        self.a_min = self.v_min = None      # min(arrival - capture) in ms
        self.a_max = None
        self.video_bytes_since_audio = 0
        self.samples = []                   # (offset ms, video bytes just before)

    def _now_ms(self):
        return (time.perf_counter() - self.t0) * 1000

    def video(self, ts_us, nbytes=0):
        self.video_bytes_since_audio += nbytes
        off = self._now_ms() - ts_us / 1000
        self.v_min = off if self.v_min is None else min(self.v_min, off)

    def audio(self, ts_us, nbytes):
        now = self._now_ms()
        off = now - ts_us / 1000
        self.a_min = off if self.a_min is None else min(self.a_min, off)
        self.a_max = off if self.a_max is None else max(self.a_max, off)
        self.samples.append((off, self.video_bytes_since_audio))
        self.video_bytes_since_audio = 0
        ms = nbytes / 192.0                 # 48 kHz * 2 ch * 2 bytes = 192 bytes per ms
        self.chunks += 1
        self.chunk_ms_sum += ms
        self.chunk_ms_max = max(self.chunk_ms_max, ms)
        if self.last_audio_arrival is not None:
            self.gap_max = max(self.gap_max, now - self.last_audio_arrival)
        self.last_audio_arrival = now

    def report(self):
        if self._now_ms() / 1000 < self.PERIOD_S:
            return None
        text = None
        if self.chunks:
            lag = (self.a_min - self.v_min) if (self.a_min is not None and self.v_min is not None) else 0.0
            text = (f"audio pieces {self.chunks / self.PERIOD_S:.0f}/s, avg {self.chunk_ms_sum / self.chunks:.1f} ms, "
                    f"max {self.chunk_ms_max:.1f} ms, biggest gap {self.gap_max:.0f} ms, "
                    f"JITTER {self.a_max - self.a_min:.0f} ms | "
                    f"sound arrives {lag:+.0f} ms vs video (capture-to-arrival)")
        if text and self.samples:
            late = [b for o, b in self.samples if o - self.a_min > 25]
            ok = [b for o, b in self.samples if o - self.a_min <= 10]
            if late and ok:
                text += (f" | late pieces {len(late)}: video before them avg {sum(late) / len(late) / 1000:.0f} KB"
                         f" vs on-time {sum(ok) / len(ok) / 1000:.0f} KB")
        self.t0 = time.perf_counter()
        self._reset()
        return text


# --------------------------------------------------------------------------
class AudioChannel(threading.Thread):
    """Second USB connection for SOUND ONLY (iPhone port 2346). Sound then
    never waits behind big video frames. With an older iPhone app the port is
    closed and sound keeps coming on the main connection."""
    RETRY_S = 2.0

    def __init__(self, receiver):
        super().__init__(daemon=True, name="audio-channel")
        self.rx = receiver
        self.s = receiver.s

    def run(self):
        if os.environ.get("MIRROR_AUDIO_CHANNEL", "1") == "0":   # test switch: sound on main channel
            print("[audio] sound-only channel disabled (MIRROR_AUDIO_CHANNEL=0)", flush=True)
            return
        while self.s.running:
            if self.s.state != ST_CONNECTED:
                time.sleep(0.3)
                continue
            try:
                sock = self.rx.open_socket(AUDIO_PORT)
            except Exception:
                time.sleep(self.RETRY_S)      # older iPhone app: no sound-only channel
                continue
            print(f"[audio] {time.strftime('%H:%M:%S')} sound-only USB channel connected", flush=True)
            self.s.audio_channel = "separate"
            try:
                self.stream(sock)
            except Exception as e:
                print(f"[audio] sound channel ended: {type(e).__name__}: {e}", flush=True)
            finally:
                self.s.audio_channel = "main"
                try:
                    sock.close()
                except OSError:
                    pass
            time.sleep(0.3)

    def stream(self, sock):
        sock.setblocking(True)
        sock.settimeout(SOCKET_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while self.s.running and self.s.state == ST_CONNECTED:
            ptype, flags, ts_us, length = HEADER.unpack(Receiver.recv_exact(sock, HEADER.size))
            if length > MAX_PAYLOAD:
                raise ValueError(f"bad packet length {length}")
            payload = Receiver.recv_exact(sock, length) if length else b""
            self.s.bytes_total += HEADER.size + length
            if ptype == T_AUDIO_PCM:
                self.rx.handle_audio(ts_us, bytes(payload))
            elif ptype == T_AUDIO_MIC:
                self.rx.handle_mic(ts_us, bytes(payload))


# --------------------------------------------------------------------------
class Receiver(threading.Thread):
    def __init__(self, shared: Shared, tcp_address=None):
        super().__init__(daemon=True, name="receiver")
        self.s = shared
        self.tcp_address = tcp_address
        self.view = FrameRenderer("bgra")       # for the window
        self.sync = SyncMeter()
        self.video = VideoWorker(self)
        self.video.start()
        self.audio_channel = AudioChannel(self)
        self.audio_channel.start()

    # ---- connection -------------------------------------------------------
    @staticmethod
    async def _usb_connect(port):
        devices = await usbmux.list_devices()
        usb = [d for d in devices if d.is_usb]
        if not usb:
            raise NoDeviceError()
        return await usb[0].connect(port)

    def open_socket(self, port=DEVICE_PORT):
        if self.tcp_address:
            host, base = self.tcp_address
            return socket.create_connection((host, base + (port - DEVICE_PORT)), timeout=2)
        # Selector loop: keeps the socket free of Windows IOCP so we can use
        # it as a normal blocking socket afterwards.
        return asyncio.run(self._usb_connect(port), loop_factory=asyncio.SelectorEventLoop)

    def run(self):
        while self.s.running:
            try:
                sock = self.open_socket()
            except NoDeviceError:
                self.s.state = ST_NO_USB
                time.sleep(1.0)
                continue
            except Exception:
                self.s.state = ST_NO_APP
                time.sleep(1.0)
                continue

            self.s.state = ST_CONNECTED
            self.s.connected_since = time.monotonic()
            print(f"[mirror] {time.strftime('%H:%M:%S')} connected to iPhone", flush=True)
            try:
                self.stream(sock)
            except Exception as e:  # disconnect, timeout, protocol error
                print(f"[mirror] {time.strftime('%H:%M:%S')} stream ended: {type(e).__name__}: {e}", flush=True)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                self.s.state = ST_NO_APP
                self.video.reset()
                self.s.clear_frame()
                self.s.codec = ""
                if self.s.audio is not None:
                    self.s.audio.reset()
            time.sleep(0.3)

    # ---- streaming --------------------------------------------------------
    @staticmethod
    def recv_exact(sock, n):
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            r = sock.recv_into(view[got:], n - got)
            if r == 0:
                raise ConnectionError("iPhone closed the connection")
            got += r
        return buf

    def stream(self, sock):
        """Network loop: only reads packets and hands them out.
        Audio is played immediately; video goes to the VideoWorker thread,
        so a slow video frame can never delay the sound."""
        sock.setblocking(True)
        sock.settimeout(SOCKET_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # bigger socket buffer: absorbs 20-30 Mbps bursts (keyframes)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)

        self.video.reset()               # fresh decoder for each connection
        config = b""                     # Annex-B SPS+PPS
        sync = self.sync = SyncMeter()

        while self.s.running:
            ptype, flags, ts_us, length = HEADER.unpack(self.recv_exact(sock, HEADER.size))
            if length > MAX_PAYLOAD:
                raise ValueError(f"bad packet length {length}")
            payload = self.recv_exact(sock, length) if length else b""
            self.s.bytes_total += HEADER.size + length

            if ptype == T_VIDEO_CONFIG:
                if length >= 4:
                    new_config = bytes(payload[4:])
                    if new_config != config:
                        w, h = struct.unpack(">HH", bytes(payload[:4]))
                        self.s.codec = describe_sps(new_config)
                        print(f"[mirror] {time.strftime('%H:%M:%S')} stream: {self.s.codec}, {w}x{h}", flush=True)
                    config = new_config
            elif ptype == T_VIDEO_FRAME:
                self.sync.video(ts_us, length)
                is_key = bool(flags & FLAG_KEYFRAME)
                data = (config + bytes(payload)) if is_key else bytes(payload)
                self.video.submit(data, is_key, ts_us)
            elif ptype == T_ORIENTATION:
                if length >= 1:
                    self.s.orientation = payload[0]
            elif ptype == T_AUDIO_PCM:
                self.handle_audio(ts_us, bytes(payload))
            elif ptype == T_AUDIO_MIC:
                self.handle_mic(ts_us, bytes(payload))
            elif ptype == T_INFO:
                text = bytes(payload).decode("utf-8", "replace")
                self.s.info = text
                print(f"[iphone] {time.strftime('%H:%M:%S')} {text}", flush=True)
            # T_HEARTBEAT: nothing to do (it just keeps the timeout from firing)
            report = self.sync.report()
            if report:
                self.s.sync_text = report
                print(f"[sync] {time.strftime('%H:%M:%S')} {report}", flush=True)

    def handle_audio(self, ts_us, pcm):
        """Sound from either channel: play at once + record."""
        self.sync.audio(ts_us, len(pcm))
        self.s.last_audio_ts = ts_us
        if self.s.audio is not None:
            self.s.audio.feed(ts_us, pcm)
        rec = self.s.recorder
        if rec is not None and rec.active:
            rec.audio(ts_us, pcm)

    def handle_mic(self, ts_us, pcm):
        """iPhone microphone: only to the stream (never the laptop speakers)."""
        audio = self.s.audio
        if audio is not None and hasattr(audio, "feed_mic"):
            audio.feed_mic(ts_us, pcm)

    # ---- output -----------------------------------------------------------
    def present(self, frame, k):
        """Rotate + fit the decoded frame to the video area, publish it (BGRA)."""
        fw, fh = frame.width, frame.height
        rw, rh = (fh, fw) if k % 2 else (fw, fh)        # size after rotation
        tw, th = self.s.target_size
        scale = min(tw / rw, th / rh)
        if abs(scale - 1.0) < 0.005:                      # 1:1 - keep every pixel
            ow, oh = rw, rh
        else:
            ow, oh = max(2, int(rw * scale) & ~1), max(2, int(rh * scale) & ~1)
        img = self.view.render(frame, k, ow, oh, self.s.sharpen)
        with self.s.lock:
            self.s.frame = img
            self.s.frame_seq += 1
            self.s.frames_decoded += 1
            self.s.video_size = (rw, rh)
        self.s.frame_ready.set()

    def snapshot(self, frame, k, path, callback):
        """Full-resolution upright picture -> PNG (saved in a helper thread)."""
        try:
            rw, rh = (frame.height, frame.width) if k % 2 else (frame.width, frame.height)
            rgb = FrameRenderer("rgb24").render(frame, k, rw, rh)
        except Exception as e:
            callback(path, f"{type(e).__name__}: {e}")
            return

        def save():
            try:
                import os
                import pygame
                os.makedirs(os.path.dirname(path), exist_ok=True)
                surf = pygame.image.frombuffer(rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), "RGB")
                pygame.image.save(surf, path)
                callback(path, "")
            except Exception as e:
                callback(path, f"{type(e).__name__}: {e}")
        threading.Thread(target=save, daemon=True, name="snapshot").start()
