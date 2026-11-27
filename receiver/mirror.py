"""
mirror.py - iPhone USB Mirror receiver (Windows).

Connects to the iPhone's Broadcast Extension (TCP port 2345 on the phone)
through usbmux (Apple Mobile Device service) over the USB cable, decodes the
H.264 video and shows it in a window called "iPhone USB Mirror" for OBS
Window Capture.

Game sound plays on the default Windows output device (OBS "Desktop Audio").

Keys:  F11 fullscreen | Esc leave fullscreen | F1 stats overlay
       M  mute / unmute game sound
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
    def __init__(self, shared: Shared, tcp_address=None):
        super().__init__(daemon=True, name="receiver")
        self.s = shared
        self.tcp_address = tcp_address

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
                    config = bytes(payload[4:])
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
        """Scale + rotate the decoded frame to fit the window, then publish it."""
        k = ROTATION_K.get(self.s.orientation, 0)
        if self.s.flip_landscape and k in (1, 3):
            k = (k + 2) % 4
        fw, fh = frame.width, frame.height
        rw, rh = (fh, fw) if k % 2 else (fw, fh)        # size after rotation
        tw, th = self.s.target_size
        scale = min(tw / rw, th / rh)
        ow, oh = max(2, int(rw * scale) & ~1), max(2, int(rh * scale) & ~1)
        # size to ask swscale for (before rotation)
        sw, sh = (oh, ow) if k % 2 else (ow, oh)
        rgb = frame.reformat(width=sw, height=sh, format="rgb24", interpolation="BILINEAR").to_ndarray()
        # PyAV rows can be padded (and rot90 returns a view): pygame needs a
        # tightly packed C-contiguous buffer.
        rgb = np.ascontiguousarray(np.rot90(rgb, k) if k else rgb)
        with self.s.lock:
            self.s.frame = rgb
            self.s.frame_seq += 1
            self.s.frames_decoded += 1
            self.s.video_size = (rw, rh)


# --------------------------------------------------------------------------
# Window (main thread)
# --------------------------------------------------------------------------
def load_settings(shared):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            shared.flip_landscape = bool(json.load(f).get("flip_landscape", False))
    except (OSError, ValueError):
        pass


def save_settings(shared):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"flip_landscape": shared.flip_landscape}, f)
    except OSError:
        pass


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
    windowed_size = (1280, 720)
    screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE)
    fullscreen = False
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
                elif event.key == pygame.K_F1:
                    show_stats = not show_stats
                elif event.key == pygame.K_m:
                    muted = shared.audio.toggle_mute()
                    print(f"[audio] {'muted' if muted else 'unmuted'}", flush=True)
                elif event.key == pygame.K_r:
                    shared.flip_landscape = not shared.flip_landscape
                    save_settings(shared)

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
                current = (pygame.image.frombuffer(arr, (w, h), "RGB"), arr)
                fps_frames += 1

        screen.fill((0, 0, 0))
        if current is not None:
            surf = current[0]
            sw, sh = surf.get_size()
            # Window was resized since this frame was made -> quick rescale.
            scale = min(win_w / sw, win_h / sh)
            if abs(scale - 1.0) > 0.01:
                surf = pygame.transform.scale(surf, (max(1, int(sw * scale)), max(1, int(sh * scale))))
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
                "F11 fullscreen  M mute  R flip  F1 hide",
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
