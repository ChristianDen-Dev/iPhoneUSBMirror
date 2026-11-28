"""
core.py - the "engine" of the receiver (no window code here):

  Receiver thread : finds a phone and streams from it, in this order:
                      1. iPhone over USB  (usbmux, port 2345)
                      2. Android over USB or Wi-Fi (adb + scrcpy-server, android.py)
                      3. iPhone over Wi-Fi (TCP to the iPhone's IP, port 2345)
                    reads packets, plays audio at once, hands video on.
  WiFiFinder      : looks for the iPhone app on the local network.
  VideoWorker     : decodes H.264 on the GPU (CPU fallback), always shows
                    the NEWEST frame (never builds a delay), feeds the
                    recorder and takes screenshots.
  Shared          : state shared with the window thread.

Protocol: see docs/PROTOCOL.md
"""

import asyncio
import collections
import ipaddress
import os
import re
import select
import socket
import struct
import threading
import time
from fractions import Fraction

import av
from av.codec.hwaccel import HWAccel
import numpy as np
from pymobiledevice3 import usbmux

import android

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
        # which phone / link (shown in the window)
        self.device_kind = ""        # "iPhone" / "Android"
        self.device_name = ""
        self.link_name = ""          # "USB" / "Wi-Fi"
        self.found = ""              # waiting screen hint: "iphone" / "android_unauth" / "android_error"
        self.hint = ""               # extra text for the hint
        self.control = None          # android.AndroidControl while an Android phone is connected
        self.notices = collections.deque()   # (text, kind) messages for the window
        # Wi-Fi settings (the window loads/saves them)
        self.link_mode = "auto"      # "auto" (USB first, then Wi-Fi) / "usb" (cable only) / "wifi" (Wi-Fi only)
        self.wifi_hosts = []         # iPhone IPs that worked before (newest first)
        self.android_hosts = []      # Android IPs switched to Wi-Fi before
        self.android_keep_sound = False   # Android 13+: sound also stays on the phone
        self.hosts_changed = False
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

    @property
    def usb_allowed(self):
        return self.link_mode != "wifi"

    @property
    def wifi_allowed(self):
        return self.link_mode != "usb"

    def remember(self, attr, host):
        hosts = [h for h in getattr(self, attr) if h != host]
        setattr(self, attr, ([host] + hosts)[:4])
        self.hosts_changed = True

    def notice(self, text, kind="info"):
        self.notices.append((text, kind))
        print(f"[link] {time.strftime('%H:%M:%S')} {text}", flush=True)

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
            if self.s.state != ST_CONNECTED or self.s.device_kind not in ("iPhone", "Android app"):
                time.sleep(0.3)
                continue
            try:
                sock = self.rx.open_socket(AUDIO_PORT)
            except Exception:
                time.sleep(self.RETRY_S)      # older iPhone app: no sound-only channel
                continue
            print(f"[audio] {time.strftime('%H:%M:%S')} sound-only channel connected ({self.s.link_name})", flush=True)
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
class WiFiFinder(threading.Thread):
    """Finds the iPhone app on the local network while nothing is connected:
    remembered IPs, the iPhone Hotspot address, and a quick scan of the
    laptop's own /24 network(s) for port 2345."""
    SCAN_EVERY_S = 15
    HOTSPOT_IPHONE = "172.20.10.1"      # the iPhone itself when the laptop uses its Hotspot

    def __init__(self, shared):
        super().__init__(daemon=True, name="wifi-finder")
        self.s = shared
        self.found = []
        self.iphones = []                   # Apple devices seen on Wi-Fi (lockdown port 62078)
        self.scan_now = threading.Event()

    @staticmethod
    def local_ipv4s():
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        try:                                     # address of the default route (sends nothing)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect(("10.255.255.255", 1))
                ips.add(u.getsockname()[0])
        except OSError:
            pass
        out = []
        for ip in ips:
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback and not a.is_link_local:
                out.append(ip)
        return out

    def candidates(self):
        hosts = list(self.s.wifi_hosts) + list(self.found)
        if any(ip.startswith("172.20.10.") for ip in self.local_ipv4s()):
            hosts.insert(0, self.HOTSPOT_IPHONE)
        seen, out = set(), []
        for h in hosts:
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out[:6]

    def scan(self, port=DEVICE_PORT):
        """Non-blocking connect to x.x.x.1-254:<port> on each local /24 (~1 s)."""
        mine = self.local_ipv4s()
        nets = sorted({ip.rsplit(".", 1)[0] for ip in mine})[:4]
        hits = []
        for net in nets:
            socks = {}
            for i in range(1, 255):
                ip = f"{net}.{i}"
                if ip in mine:
                    continue
                so = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                so.setblocking(False)
                try:
                    so.connect_ex((ip, port))
                    socks[so] = ip
                except OSError:
                    so.close()
            deadline = time.monotonic() + 0.9
            pending = set(socks)
            while pending and time.monotonic() < deadline:
                _, w, x = select.select([], list(pending), list(pending), max(0.0, deadline - time.monotonic()))
                for so in w:
                    if so.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                        hits.append(socks[so])
                for so in set(w) | set(x):
                    pending.discard(so)
            for so in socks:
                so.close()
        return hits

    def run(self):
        while self.s.running:
            if self.s.wifi_allowed and self.s.state != ST_CONNECTED:
                try:
                    hits = self.scan()
                    # every iPhone on Wi-Fi answers on 62078 - tells "phone is there, broadcast is not"
                    self.iphones = [] if hits else self.scan(62078)
                except Exception as e:
                    print(f"[wifi] scan failed: {e}", flush=True)
                    hits = []
                new = [h for h in hits if h not in self.found]
                if new:
                    print(f"[wifi] {time.strftime('%H:%M:%S')} iPhone app answers at {', '.join(new)}", flush=True)
                self.found = hits
            self.scan_now.wait(self.SCAN_EVERY_S)
            self.scan_now.clear()


# --------------------------------------------------------------------------
class Receiver(threading.Thread):
    def __init__(self, shared: Shared, tcp_address=None):
        super().__init__(daemon=True, name="receiver")
        self.s = shared
        self.tcp_address = tcp_address
        self.link = ("usb",)                    # ("usb",) / ("wifi", host, base_port) /
                                                # ("adb", serial, main_local, audio_local) = Android app
        self.view = FrameRenderer("bgra")       # for the window
        self.sync = SyncMeter()
        self.video = VideoWorker(self)
        self.video.start()
        self.audio_channel = AudioChannel(self)
        self.audio_channel.start()
        self.wifi = WiFiFinder(shared)
        self.wifi.start()
        self.android_ok = android.available()
        self.android_session = None
        self._android_retry_at = 0.0
        self._handoff = None                    # (socket, link) found while on Wi-Fi / scrcpy
        self._mouse = None                      # scrcpy mouse helper next to the Android app
        self._adb_lost_at = 0.0                 # Android link dropped (for the adb self-repair)
        self._adb_restart_at = 0.0
        self._android_switching = False         # Wi-Fi-only mode: turning a USB Android into Wi-Fi
        self._sock = None
        if not self.android_ok:
            print("[android] adb / scrcpy-server not found in vendor/android - Android support off", flush=True)

    # ---- connection -------------------------------------------------------
    @staticmethod
    async def _usb_connect(port):
        try:
            devices = await usbmux.list_devices()
        except Exception:                       # Apple Devices / iTunes not installed or not running
            raise NoDeviceError()
        usb = [d for d in devices if d.is_usb]
        if not usb:
            raise NoDeviceError()
        return await usb[0].connect(port)

    def usb_socket(self, port=DEVICE_PORT):
        # Selector loop: keeps the socket free of Windows IOCP so we can use
        # it as a normal blocking socket afterwards.
        return asyncio.run(self._usb_connect(port), loop_factory=asyncio.SelectorEventLoop)

    def open_socket(self, port=DEVICE_PORT):
        """Socket to the iPhone app over the CURRENT link (USB or Wi-Fi)."""
        if self.link[0] == "wifi":
            host, base = self.link[1], self.link[2]
            return socket.create_connection((host, base + (port - DEVICE_PORT)), timeout=2)
        if self.link[0] == "adb":                       # Android app through adb forward
            local = self.link[2] if port == DEVICE_PORT else self.link[3]
            if not local:
                raise OSError("no adb forward for this port")
            s = socket.create_connection(("127.0.0.1", local), timeout=2)
            s.settimeout(1.5)
            try:
                ok = s.recv(1, socket.MSG_PEEK)        # forward to a closed port: closes at once
            except OSError:
                ok = b""
            if not ok:
                s.close()
                raise OSError("app port closed")
            return s
        return self.usb_socket(port)

    def try_iphone_wifi(self):
        if self.tcp_address:                    # debug: --tcp HOST:PORT
            host, base = self.tcp_address
            try:
                sock = socket.create_connection((host, base), timeout=2)
            except OSError:
                return None
            self.link = ("wifi", host, base)
            return sock
        if not self.s.wifi_allowed:
            return None
        for host in self.wifi.candidates():
            try:
                sock = socket.create_connection((host, DEVICE_PORT), timeout=0.6)
            except OSError:
                continue
            self.link = ("wifi", host, DEVICE_PORT)
            return sock
        return None

    def run(self):
        while self.s.running:
            try:
                self.connect_once()
            except Exception:
                import traceback
                print(f"[mirror] {time.strftime('%H:%M:%S')} connection loop error (recovering):", flush=True)
                traceback.print_exc()
                try:
                    self._sock = None
                    self.cleanup()
                except Exception:
                    pass
                time.sleep(1.0)

    def connect_once(self):
        """One round: find a phone and mirror it until it disconnects."""
        handoff, self._handoff = self._handoff, None
        sock, iphone_on_usb = None, False
        if handoff is not None:
            sock, self.link = handoff
        elif not self.tcp_address and self.s.usb_allowed:
            try:
                sock = self.usb_socket()
                self.link = ("usb",)
            except NoDeviceError:
                pass
            except Exception:
                iphone_on_usb = True        # iPhone plugged in, broadcast not started
        if sock is None and self.android_ok and not self.tcp_address:
            if self.try_android():
                return
        if sock is None:
            sock = self.try_iphone_wifi()
        if sock is None:
            if iphone_on_usb:
                self.s.found = "iphone"
            elif self.s.wifi_allowed and self.wifi.iphones and self.s.found in ("", "iphone", "iphone_wifi"):
                self.s.found, self.s.hint = "iphone_wifi", ", ".join(self.wifi.iphones[:2])
            elif self.s.found in ("iphone", "iphone_wifi"):
                self.s.found = ""
            self.s.state = ST_NO_APP if self.s.found else ST_NO_USB
            time.sleep(1.0)
            return
        self.run_iphone(sock)
        time.sleep(0.3)

    def run_iphone(self, sock):
        """One session with a Den-Dev Live app: iPhone (USB / Wi-Fi) or the
        Android app (adb over USB, or Wi-Fi). Same wire format for all."""
        s = self.s
        kind = self.link[0]
        wifi = kind == "wifi" or (kind == "adb" and android.is_wifi_serial(self.link[1]))
        s.state = ST_CONNECTED
        s.found, s.hint = "", ""
        if kind == "adb":
            s.device_kind, s.device_name = "Android app", "Android"
        else:
            s.device_kind, s.device_name = "iPhone", "iPhone"   # an Android app on Wi-Fi says so in INFO
        s.link_name = "Wi-Fi" if wifi else "USB"
        s.orientation = 1
        s.connected_since = time.monotonic()
        self._sock = sock
        if kind == "wifi":
            where = f"over Wi-Fi ({self.link[1]})"
        elif kind == "adb":
            where = f"over {s.link_name} (adb {self.link[1]})"
        else:
            where = "over USB"
        print(f"[mirror] {time.strftime('%H:%M:%S')} connected to the "
              f"{'Android app' if kind == 'adb' else 'app'} {where}", flush=True)
        if kind == "wifi" and not self.tcp_address:
            s.remember("wifi_hosts", self.link[1])
        if kind == "wifi" and not self.tcp_address and s.link_mode == "auto":
            threading.Thread(target=self._watch_usb, args=(sock,), daemon=True, name="usb-watch").start()
        if kind == "adb":
            threading.Thread(target=self._mouse_loop, args=(sock, self.link[1]), daemon=True,
                             name="android-mouse").start()
        link = self.link
        try:
            self.stream(sock)
        except Exception as e:  # disconnect, timeout, protocol error
            print(f"[mirror] {time.strftime('%H:%M:%S')} stream ended: {type(e).__name__}: {e}", flush=True)
            if kind == "adb" and self._handoff is None:
                self._adb_lost_at = time.monotonic()
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            if kind == "adb":
                self._stop_mouse()
                android.remove_forward(link[1], link[2])
                android.remove_forward(link[1], link[3])
            self.cleanup()

    # ---- Android app: mouse control (small scrcpy helper) ---------------------
    def _mouse_loop(self, sock, serial):
        """While the Android app mirrors over adb: keep a tiny scrcpy helper
        running so the mouse works as a finger (the picture comes from the app)."""
        time.sleep(1.0)
        while self.s.running and self._sock is sock:
            m = self._mouse
            if m is None or not m.alive:
                if m is not None:
                    m.stop()
                m = android.AndroidSession(serial, control_only=True)
                try:
                    m.start()
                except Exception as e:
                    m.stop()
                    self._mouse = None
                    print(f"[android] mouse control not available: {e}", flush=True)
                    time.sleep(10)
                    continue
                if self._sock is not sock:
                    m.stop()
                    break
                self._mouse = m
                self.s.control = m.control
                print(f"[android] {time.strftime('%H:%M:%S')} mouse control ready", flush=True)
            time.sleep(3)

    def _stop_mouse(self):
        m, self._mouse = self._mouse, None
        if m is not None:
            m.stop()
        self.s.control = None

    def _usb_app_android(self):
        """(socket, link) of the Android app over a USB adb link, or None."""
        if not self.android_ok:
            return None
        for serial, st in android.list_devices():
            if st == "device" and not android.is_wifi_serial(serial):
                got = android.connect_app(serial)
                if got:
                    return got[0], ("adb", serial, got[1], got[2])
        return None

    def _watch_usb(self, wifi_sock):
        """While on Wi-Fi: as soon as the phone is plugged in, move to USB
        (lower delay, no Wi-Fi stutters). iPhone: usbmux. Android app: adb."""
        while self.s.running and self._sock is wifi_sock and self.s.link_mode == "auto":
            time.sleep(2.0)
            if self.s.device_kind == "Android app":
                found = self._usb_app_android()
            else:
                try:
                    found = (self.usb_socket(), ("usb",))
                except Exception:
                    found = None
            if found is None:
                continue
            if self._sock is not wifi_sock:
                found[0].close()
                return
            self.s.notice("USB cable found - switching from Wi-Fi to USB", "good")
            self._handoff = found
            try:
                wifi_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

    def cleanup(self):
        s = self.s
        s.state = ST_NO_APP if s.found else ST_NO_USB
        s.device_kind = s.link_name = ""
        s.control = None
        self.video.reset()
        s.clear_frame()
        s.codec = ""
        s.info = ""
        if s.audio is not None:
            s.audio.reset()

    # ---- Android --------------------------------------------------------------
    def try_android(self):
        """Mirror an Android phone if one is ready. True = a session ran."""
        devs = android.list_devices()
        ready = [d for d, st in devs if st == "device"]
        mode = self.s.link_mode
        if mode == "usb":
            ready = [d for d in ready if not android.is_wifi_serial(d)]
        elif mode == "wifi":
            on_usb = [d for d in ready if not android.is_wifi_serial(d)]
            ready = [d for d in ready if android.is_wifi_serial(d)]
            if not ready and on_usb and not self._android_switching:
                self._switch_android_to_wifi(on_usb[0])     # Wi-Fi only: do it for the user
                return False
        if not ready:
            now = time.monotonic()
            # The phone's USB link blinked and adb lost it: give adb a fresh start (once).
            if self._adb_lost_at and now - self._adb_lost_at > 4 and now - self._adb_restart_at > 30:
                self._adb_lost_at, self._adb_restart_at = 0.0, now
                print(f"[android] {time.strftime('%H:%M:%S')} phone gone after a drop - restarting adb", flush=True)
                android.restart_adb()
                return False
            if any(st == "offline" for _, st in devs):
                android.adb("reconnect", "offline", timeout=8)
            if any(st == "unauthorized" for _, st in devs):
                self.s.found = "android_unauth"
            elif self.s.found.startswith("android"):
                self.s.found, self.s.hint = "", ""
            now = time.monotonic()
            if self.s.wifi_allowed and self.s.android_hosts and now >= self._android_retry_at:   # phone switched to Wi-Fi before
                self._android_retry_at = now + 10
                for host in list(self.s.android_hosts)[:2]:
                    threading.Thread(target=android.connect_wifi, args=(host,), daemon=True).start()
            return False
        self._adb_lost_at = 0.0
        # 1. The Den-Dev Live app (same as the iPhone: picture + game sound + mic).
        for serial in sorted(ready, key=android.is_wifi_serial):  # USB first
            got = android.connect_app(serial)
            if got:
                self.link = ("adb", serial, got[1], got[2])
                self.run_iphone(got[0])
                return True
        # 2. Fallback: app mirror not started -> scrcpy (picture + game sound, no mic).
        serial = sorted(ready, key=android.is_wifi_serial)[0]     # USB first
        sess = android.AndroidSession(serial, self.s.android_keep_sound)
        try:
            sess.start()
        except Exception as e:
            print(f"[android] {time.strftime('%H:%M:%S')} could not start on {serial}: {e}", flush=True)
            sess.stop()
            self.s.found, self.s.hint = "android_error", str(e)[:120]
            self.s.state = ST_NO_APP
            time.sleep(3.0)
            return True
        self.run_android(sess)
        time.sleep(0.5)
        return True

    def run_android(self, sess):
        s = self.s
        self.android_session = sess
        s.state = ST_CONNECTED
        s.found, s.hint = "", ""
        s.device_kind, s.device_name = "Android", sess.name
        s.link_name = "Wi-Fi" if sess.wifi else "USB"
        s.orientation = 1                       # Android sends upright pictures
        s.codec = "H.264"
        s.info = f"Android {sess.name} (API {sess.sdk}) | sound {'on' if sess.audio_ok else 'off'}"
        s.connected_since = time.monotonic()
        self.video.reset()
        self.sync = SyncMeter()
        print(f"[mirror] {time.strftime('%H:%M:%S')} connected to Android {sess.name} over {s.link_name} "
              f"({sess.serial}, API {sess.sdk}, sound {'on' if sess.audio_ok else 'off'})", flush=True)
        if sess.audio_ok:
            threading.Thread(target=self._android_audio, args=(sess,), daemon=True, name="android-audio").start()
        threading.Thread(target=self._watch_app, args=(sess,), daemon=True, name="android-app-watch").start()
        config = b""
        try:
            for item in sess.read_video():
                if not s.running:
                    break
                if item[0] == "size":
                    sess.control.screen = item[1]
                    print(f"[android] picture {item[1][0]}x{item[1][1]}", flush=True)
                    continue
                kind, is_key, pts, data = item
                s.bytes_total += 12 + len(data)
                if kind == "config":
                    config = data
                    s.codec = describe_sps(config)
                    s.control = sess.control
                    continue
                self.sync.video(pts, len(data))
                self.video.submit(config + data if is_key else data, is_key, pts)
        except Exception as e:
            print(f"[mirror] {time.strftime('%H:%M:%S')} Android stream ended: {type(e).__name__}: {e}", flush=True)
            if self._handoff is None:
                self._adb_lost_at = time.monotonic()
        finally:
            sess.stop()
            self.android_session = None
            self.cleanup()

    def _watch_app(self, sess):
        """scrcpy fallback running: as soon as the user taps Start mirroring in
        the Den-Dev Live app, switch to the app (adds the mic, survives USB blinks)."""
        while self.s.running and self.android_session is sess:
            time.sleep(3.0)
            if self.android_session is not sess:
                return
            got = android.connect_app(sess.serial)
            if not got:
                continue
            if self.android_session is not sess:
                got[0].close()
                return
            self.s.notice("Den-Dev Live app found - switching to the app", "good")
            self._handoff = (got[0], ("adb", sess.serial, got[1], got[2]))
            sess.stop()
            return

    def _android_audio(self, sess):
        try:
            for pts, pcm in sess.read_audio():
                self.s.bytes_total += 12 + len(pcm)
                self.handle_audio(pts, pcm)
        except Exception:
            pass                                # the video loop reports the end

    def _switch_android_to_wifi(self, serial):
        self._android_switching = True

        def work():
            try:
                self.s.notice("Wi-Fi only: moving the Android phone to Wi-Fi...", "info")
                ip, msg = android.enable_wifi(serial)
                if ip:
                    self.s.remember("android_hosts", ip)
                    self.s.notice(f"Android on Wi-Fi ({ip}) - you can unplug the cable", "good")
                else:
                    self.s.notice(f"Android Wi-Fi failed: {msg}", "warn")
            finally:
                self._android_switching = False
        threading.Thread(target=work, daemon=True, name="android-wifi").start()

    def set_link_mode(self, mode):
        """Auto / USB only / Wi-Fi only. Drops a connection the new mode does not allow."""
        s = self.s
        s.link_mode = mode
        wrong = ((mode == "usb" and s.link_name == "Wi-Fi") or (mode == "wifi" and s.link_name == "USB"))
        if s.state == ST_CONNECTED and wrong:
            if self.android_session:
                self.android_restart()
            elif self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if s.wifi_allowed:
            self.wifi.scan_now.set()

    def android_restart(self):
        """Reconnect the Android phone (new settings take effect)."""
        sess = self.android_session
        if sess:
            sess.stop()

    def android_to_wifi(self):
        """Android on USB -> also on Wi-Fi; afterwards the cable can be unplugged."""
        sess = self.android_session
        if not sess or sess.wifi:
            self.s.notice("Connect the Android phone with USB first", "warn")
            return

        def work():
            self.s.notice("Switching the Android phone to Wi-Fi...", "info")
            ip, msg = android.enable_wifi(sess.serial)
            if ip:
                self.s.remember("android_hosts", ip)
                self.s.notice(f"Android on Wi-Fi ({ip}) - you can unplug the cable now", "good")
            else:
                self.s.notice(f"Android Wi-Fi failed: {msg}", "warn")
        threading.Thread(target=work, daemon=True, name="android-wifi").start()

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
                if text.startswith("Android "):           # the Den-Dev Live Android app (Wi-Fi mirror)
                    self.s.device_kind = "Android app"      # same protocol as the iPhone (not adb)
                    self.s.device_name = text.split(" (", 1)[0][len("Android "):] or "Android"
                print(f"[iphone]{time.strftime('%H:%M:%S')} {text}", flush=True)
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
