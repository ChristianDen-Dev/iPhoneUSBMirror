"""
mirror.py - iPhone USB Mirror receiver (Windows).

Connects to the iPhone's Broadcast Extension (TCP port 2345 on the phone)
through usbmux (Apple Mobile Device service) over the USB cable, decodes the
H.264 video and shows it in a window called "iPhone USB Mirror" for OBS
Window Capture.

Game sound plays on the default Windows output device (OBS "Desktop Audio").

Keys:  F2  STREAM MODE: borderless window at the iPhone's exact pixel size
           (1:1, no scaling) pinned top-left - best quality for OBS
       F11 fullscreen | Esc leave fullscreen / stream mode | F1 stats overlay
       M  mute / unmute game sound
       S  sharpening on/off (used when the iPhone picture is enlarged)
       R  flip landscape 180 deg (if the game shows upside-down)

Debug: --tcp 127.0.0.1:2345 connects to a plain TCP address instead of usbmux
       (used with tools\\fake_iphone.py for testing without the phone).

Protocol: see docs/PROTOCOL.md
"""

import argparse
import asyncio
import ctypes
import json
import os
import socket
import struct
import sys
import threading
import time
from fractions import Fraction

import av
import numpy as np
import pygame
from pymobiledevice3 import usbmux

from audio_player import AudioPlayer

WINDOW_TITLE = "iPhone USB Mirror"
DEVICE_PORT = 2345
SOCKET_TIMEOUT_S = 3.0          # iPhone sends a heartbeat every 1 s
MAX_PAYLOAD = 8 * 1024 * 1024   # sanity limit for one packet

HEADER = struct.Struct(">BBQI")  # type, flags, timestamp_us, length
T_VIDEO_CONFIG, T_VIDEO_FRAME, T_AUDIO_PCM, T_ORIENTATION, T_HEARTBEAT, T_INFO = 1, 2, 3, 4, 5, 6
FLAG_KEYFRAME = 0x01

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# CGImagePropertyOrientation -> number of 90 deg counter-clockwise turns (np.rot90 k)
#   1 up: 0 | 3 down: 180 | 6: 90 CCW | 8: 90 CW (= 3 CCW)
# (6/8 directions confirmed on a real iPhone 14 Pro Max with Mobile Legends)
ROTATION_K = {1: 0, 2: 0, 3: 2, 4: 2, 5: 3, 6: 1, 7: 1, 8: 3}


H264_PROFILES = {66: "Baseline", 77: "Main", 88: "Extended", 100: "High"}


def describe_sps(annexb: bytes) -> str:
    """'H.264 High' etc. from the SPS inside an Annex-B parameter-set blob."""
    i = annexb.find(b"\x00\x00\x01")
    while i != -1 and i + 4 < len(annexb):
        nal = annexb[i + 3]
        if nal & 0x1F == 7:                     # SPS: profile_idc follows the NAL header
            prof = annexb[i + 4]
            return f"H.264 {H264_PROFILES.get(prof, prof)}"
        i = annexb.find(b"\x00\x00\x01", i + 3)
    return "H.264 (unknown profile)"


class NoDeviceError(Exception):
    pass


# --------------------------------------------------------------------------
# Shared state between the network thread and the window (main) thread.
# --------------------------------------------------------------------------
class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = True
        self.status = "Starting..."
        self.frame = None            # newest RGB numpy array (H, W, 3), ready to show
        self.frame_seq = 0
        self.target_size = (1280, 720)  # current window size (set by main thread)
        self.orientation = 1
        self.flip_landscape = False
        self.sharpen = True          # unsharp mask when enlarging small frames
        self.stream_mode = False     # F2: 1:1 borderless window for OBS
        # stats
        self.bytes_total = 0
        self.frames_decoded = 0
        self.video_size = (0, 0)
        self.decode_ms = 0.0
        self.info = ""               # last diagnostic text from the iPhone
        # audio
        self.audio = None            # AudioPlayer
        self.last_video_ts = 0       # iPhone timestamps (us) for A/V sync stats
        self.last_audio_ts = 0

    def set_status(self, text):
        with self.lock:
            self.status = text

    def clear_frame(self):
        with self.lock:
            self.frame = None
            self.frame_seq += 1


# --------------------------------------------------------------------------
# Network + decode thread
# --------------------------------------------------------------------------
class Receiver(threading.Thread):
    # Enlarging more than this -> Lanczos + sharpening filter.
    UPSCALE_SHARPEN_AT = 1.15
    UNSHARP = "5:5:1.0:5:5:0.0"   # luma 5x5 amount 1.0, chroma 5x5 off

    def __init__(self, shared: Shared, tcp_address=None):
        super().__init__(daemon=True, name="receiver")
        self.s = shared
        self.tcp_address = tcp_address
        self._graph = None
        self._graph_key = None
        self._pts = 0

    # np.rot90-style k (CCW quarter turns) -> FFmpeg filters
    ROTATE_FILTERS = {0: [], 1: [("transpose", "cclock")], 2: [("hflip", None), ("vflip", None)],
                      3: [("transpose", "clock")]}

    def _render(self, frame, k, out_w, out_h):
        """One FFmpeg filter graph does everything in C (fast):
        rotate (in YUV) -> scale only if needed -> optional sharpen -> BGRA."""
        rw, rh = (frame.height, frame.width) if k % 2 else (frame.width, frame.height)
        upscale = out_w / rw
        sharpen = self.s.sharpen and upscale > self.UPSCALE_SHARPEN_AT
        key = (frame.width, frame.height, frame.format.name, k, out_w, out_h, sharpen)
        if key != self._graph_key:
            g = av.filter.Graph()
            last = g.add_buffer(width=frame.width, height=frame.height,
                                format=frame.format.name, time_base=Fraction(1, 1000))
            chain = list(self.ROTATE_FILTERS[k])
            if (out_w, out_h) != (rw, rh):   # 1:1 (stream mode) skips scaling entirely
                flags = "lanczos" if upscale > 1.0 else "bilinear"
                chain.append(("scale", f"{out_w}:{out_h}:flags={flags}"))
            if sharpen:
                chain.append(("unsharp", self.UNSHARP))
            chain.append(("format", "bgra"))  # native 32-bit: fastest upload to the window
            for name, arg in chain:
                node = g.add(name, arg) if arg else g.add(name)
                last.link_to(node)
                last = node
            sink = g.add("buffersink")
            last.link_to(sink)
            g.configure()
            self._graph, self._graph_key = g, key
        self._pts += 1
        frame.pts = self._pts
        self._graph.push(frame)
        return np.ascontiguousarray(self._graph.pull().to_ndarray())

    # ---- connection -------------------------------------------------------
    async def _usb_connect(self):
        devices = await usbmux.list_devices()
        usb = [d for d in devices if d.is_usb]
        if not usb:
            raise NoDeviceError()
        return await usb[0].connect(DEVICE_PORT)

    def open_socket(self):
        if self.tcp_address:
            host, port = self.tcp_address
            return socket.create_connection((host, port), timeout=2)
        # Selector loop: keeps the socket free of Windows IOCP so we can use
        # it as a normal blocking socket afterwards.
        return asyncio.run(self._usb_connect(), loop_factory=asyncio.SelectorEventLoop)

    def run(self):
        while self.s.running:
            try:
                sock = self.open_socket()
            except NoDeviceError:
                self.s.set_status("Waiting for iPhone...\nPlug in the USB cable and unlock the iPhone.")
                time.sleep(1.0)
                continue
            except Exception:
                self.s.set_status("iPhone found.\nOpen the USB Mirror app and tap Start Broadcast.")
                time.sleep(1.0)
                continue

            self.s.set_status("Connected. Waiting for video...")
            print(f"[mirror] {time.strftime('%H:%M:%S')} connected to iPhone", flush=True)
            try:
                self.stream(sock)
            except Exception as e:  # disconnect, timeout, protocol error
                print(f"[mirror] stream ended: {type(e).__name__}: {e}")
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                self.s.clear_frame()
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
        sock.setblocking(True)
        sock.settimeout(SOCKET_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        decoder = av.CodecContext.create("h264", "r")
        decoder.thread_type = "SLICE"    # slice threads add no delay (frame threads do)
        config = b""                     # Annex-B SPS+PPS
        need_keyframe = True

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
                        print(f"[mirror] {time.strftime('%H:%M:%S')} stream: {describe_sps(new_config)}, {w}x{h}",
                              flush=True)
                    config = new_config
            elif ptype == T_VIDEO_FRAME:
                is_key = bool(flags & FLAG_KEYFRAME)
                if need_keyframe and not is_key:
                    continue                     # wait for a clean start
                data = (config + payload) if is_key else payload
                t0 = time.perf_counter()
                try:
                    frames = decoder.decode(av.Packet(bytes(data)))
                except av.FFmpegError as e:
                    print(f"[mirror] decode error: {e} - waiting for next keyframe")
                    need_keyframe = True
                    continue
                need_keyframe = False
                self.s.last_video_ts = ts_us
                for frame in frames:
                    self.present(frame)
                self.s.decode_ms = (time.perf_counter() - t0) * 1000
            elif ptype == T_ORIENTATION:
                if length >= 1:
                    self.s.orientation = payload[0]
            elif ptype == T_AUDIO_PCM:
                self.s.last_audio_ts = ts_us
                if self.s.audio is not None:
                    self.s.audio.feed(ts_us, bytes(payload))
            elif ptype == T_INFO:
                text = bytes(payload).decode("utf-8", "replace")
                self.s.info = text
                print(f"[iphone] {time.strftime('%H:%M:%S')} {text}", flush=True)
            # T_HEARTBEAT: nothing to do (it just keeps the timeout from firing)

    def present(self, frame):
        """Rotate + fit the decoded frame to the window, then publish it (BGRA)."""
        k = ROTATION_K.get(self.s.orientation, 0)
        if self.s.flip_landscape and k in (1, 3):
            k = (k + 2) % 4
        fw, fh = frame.width, frame.height
        rw, rh = (fh, fw) if k % 2 else (fw, fh)        # size after rotation
        tw, th = self.s.target_size
        scale = min(tw / rw, th / rh)
        if abs(scale - 1.0) < 0.005:                      # 1:1 - keep every pixel
            ow, oh = rw, rh
        else:
            ow, oh = max(2, int(rw * scale) & ~1), max(2, int(rh * scale) & ~1)
        img = self._render(frame, k, ow, oh)
        with self.s.lock:
            self.s.frame = img
            self.s.frame_seq += 1
            self.s.frames_decoded += 1
            self.s.video_size = (rw, rh)


# --------------------------------------------------------------------------
# Window (main thread)
# --------------------------------------------------------------------------
def load_settings(shared):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            shared.flip_landscape = bool(data.get("flip_landscape", False))
            shared.sharpen = bool(data.get("sharpen", True))
            shared.stream_mode = bool(data.get("stream_mode", False))
    except (OSError, ValueError):
        pass


def save_settings(shared):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"flip_landscape": shared.flip_landscape, "sharpen": shared.sharpen,
                       "stream_mode": shared.stream_mode}, f)
    except OSError:
        pass


def move_window_top_left():
    """Pin the (borderless) window to the top-left corner of the main screen."""
    if sys.platform != "win32":
        return
    try:
        hwnd = pygame.display.get_wm_info()["window"]
        SWP_NOSIZE, SWP_NOZORDER = 0x0001, 0x0004
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
    except Exception:
        pass


def stream_mode_size(video_size, desktop_size):
    """Exact video size (1:1); only shrunk if it doesn't fit on the screen."""
    vw, vh = video_size
    dw, dh = desktop_size
    if vw <= 0 or vh <= 0:
        return (1918, 886)  # landscape iPhone default until the first frame
    scale = min(1.0, dw / vw, dh / vh)
    return (max(2, int(vw * scale)), max(2, int(vh * scale)))


def draw_text_center(screen, font, text, color=(200, 200, 200)):
    lines = text.split("\n")
    sw, sh = screen.get_size()
    heights = [font.size(line)[1] for line in lines]
    y = (sh - sum(heights) - 8 * (len(lines) - 1)) // 2
    for line, h in zip(lines, heights):
        surf = font.render(line, True, color)
        screen.blit(surf, ((sw - surf.get_width()) // 2, y))
        y += h + 8


def main():
    parser = argparse.ArgumentParser(description="iPhone USB Mirror receiver")
    parser.add_argument("--tcp", help="debug: connect to HOST:PORT instead of usbmux")
    args = parser.parse_args()
    tcp_address = None
    if args.tcp:
        host, port = args.tcp.rsplit(":", 1)
        tcp_address = (host, int(port))

    # Sharp rendering on high-DPI laptop screens.
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    shared = Shared()
    load_settings(shared)
    shared.audio = AudioPlayer()

    pygame.init()
    pygame.display.set_caption(WINDOW_TITLE)
    desktop_size = pygame.display.get_desktop_sizes()[0]
    windowed_size = (1280, 720)
    screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE)
    fullscreen = False
    stream_size = None           # current stream-mode window size
    font = pygame.font.SysFont("segoeui", 26)
    small = pygame.font.SysFont("consolas", 16)
    clock = pygame.time.Clock()

    receiver = Receiver(shared, tcp_address)
    receiver.start()

    show_stats = False
    last_seq = -1
    current = None               # (surface, numpy array kept alive)
    fps_frames, fps_value, fps_t = 0, 0.0, time.perf_counter()
    rate_bytes, rate_value = 0, 0.0
    log_tick = 0

    while shared.running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                shared.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F11:
                    fullscreen = not fullscreen
                    if fullscreen:
                        windowed_size = screen.get_size()
                        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
                    else:
                        screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE)
                elif event.key == pygame.K_ESCAPE and fullscreen:
                    fullscreen = False
                    screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE)
                elif event.key == pygame.K_F2 or (event.key == pygame.K_ESCAPE and shared.stream_mode):
                    shared.stream_mode = not shared.stream_mode if event.key == pygame.K_F2 else False
                    save_settings(shared)
                    fullscreen = False
                    stream_size = None
                    if not shared.stream_mode:
                        screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE)
                    print(f"[mirror] stream mode {'on' if shared.stream_mode else 'off'}", flush=True)
                elif event.key == pygame.K_F1:
                    show_stats = not show_stats
                elif event.key == pygame.K_m:
                    muted = shared.audio.toggle_mute()
                    print(f"[audio] {'muted' if muted else 'unmuted'}", flush=True)
                elif event.key == pygame.K_s:
                    shared.sharpen = not shared.sharpen
                    save_settings(shared)
                    print(f"[mirror] sharpen {'on' if shared.sharpen else 'off'}", flush=True)
                elif event.key == pygame.K_r:
                    shared.flip_landscape = not shared.flip_landscape
                    save_settings(shared)

        # Stream mode: keep the borderless window at the video's exact size.
        if shared.stream_mode and not fullscreen:
            want = stream_mode_size(shared.video_size, desktop_size)
            if want != stream_size:
                if stream_size is None and screen.get_flags() & pygame.RESIZABLE:
                    windowed_size = screen.get_size()
                screen = pygame.display.set_mode(want, pygame.NOFRAME)
                move_window_top_left()
                stream_size = want

        win_w, win_h = screen.get_size()
        shared.target_size = (win_w, win_h)

        with shared.lock:
            seq, arr, status = shared.frame_seq, shared.frame, shared.status
        if seq != last_seq:
            last_seq = seq
            if arr is None:
                current = None
            else:
                h, w = arr.shape[:2]
                current = (pygame.image.frombuffer(arr, (w, h), "BGRA"), arr)
                fps_frames += 1

        screen.fill((0, 0, 0))
        if current is not None:
            surf = current[0]
            sw, sh = surf.get_size()
            # Window was resized since this frame was made -> quick rescale.
            scale = min(win_w / sw, win_h / sh)
            if abs(scale - 1.0) > 0.01:
                surf = pygame.transform.smoothscale(surf, (max(1, int(sw * scale)), max(1, int(sh * scale))))
                sw, sh = surf.get_size()
            screen.blit(surf, ((win_w - sw) // 2, (win_h - sh) // 2))
        else:
            draw_text_center(screen, font, status)

        # Stats (hidden by default so they never show on stream)
        now = time.perf_counter()
        if now - fps_t >= 1.0:
            fps_value = fps_frames / (now - fps_t)
            total = shared.bytes_total
            rate_value = (total - rate_bytes) * 8 / (now - fps_t) / 1e6
            rate_bytes = total
            fps_frames, fps_t = 0, now
            log_tick += 1
            if current is not None and log_tick % 5 == 0:   # console log every ~5 s
                vw, vh = shared.video_size
                a = shared.audio
                print(f"[mirror] {time.strftime('%H:%M:%S')} fps {fps_value:.1f} | {rate_value:.2f} Mbps | "
                      f"video {vw}x{vh} | orient {shared.orientation} | decode {shared.decode_ms:.1f} ms | "
                      f"audio buf {a.buffered_ms:.0f} ms, underruns {a.underruns}, skips {a.skips}"
                      f"{' MUTED' if a.muted else ''}", flush=True)
        if show_stats:
            vw, vh = shared.video_size
            lines = [
                f"FPS {fps_value:5.1f}   {rate_value:5.2f} Mbps",
                f"video {vw}x{vh}  orient {shared.orientation}  flip {'on' if shared.flip_landscape else 'off'}",
                f"decode {shared.decode_ms:4.1f} ms   window {win_w}x{win_h}",
                f"audio buf {shared.audio.buffered_ms:3.0f} ms  underruns {shared.audio.underruns}  "
                f"skips {shared.audio.skips}  {'MUTED' if shared.audio.muted else ''}  "
                f"A-V {(shared.last_audio_ts - shared.last_video_ts) / 1000:+.0f} ms",
                f"iPhone: {shared.info or '-'}",
                f"sharpen {'on' if shared.sharpen else 'off'}",
                "F2 stream mode  F11 fullscreen  M mute  S sharpen  R flip  F1 hide",
            ]
            y = 8
            for line in lines:
                t = small.render(line, True, (0, 255, 0), (0, 0, 0))
                screen.blit(t, (8, y))
                y += t.get_height() + 2

        pygame.display.flip()
        clock.tick(120)

    shared.audio.close()
    pygame.quit()


if __name__ == "__main__":
    main()
