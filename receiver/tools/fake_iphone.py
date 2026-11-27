"""
fake_iphone.py - test sender that pretends to be the iPhone Broadcast Extension.

Speaks the exact wire protocol (docs/PROTOCOL.md) on plain TCP so the receiver
can be tested without the phone:

    python tools\\fake_iphone.py                 # landscape game (orientation 6)
    python tools\\fake_iphone.py --orient 1      # portrait
    python mirror.py --tcp 127.0.0.1:2345

Like ReplayKit, the video is a PORTRAIT buffer (886x1920) and the real
orientation is sent separately. The test picture (after correct rotation):
    - red bar at the TOP, green bar on the LEFT, text "UP" readable
    - a white box moving left -> right
Also sends a 440 Hz test tone (48 kHz s16le stereo) unless --no-audio.
"""

import argparse
import math
import socket
import struct
import time
from fractions import Fraction

import av
import numpy as np

HEADER = struct.Struct(">BBQI")
T_VIDEO_CONFIG, T_VIDEO_FRAME, T_AUDIO_PCM, T_ORIENTATION, T_HEARTBEAT = 1, 2, 3, 4, 5
W, H, FPS = 886, 1920, 60        # portrait buffer, like the iPhone 14 Pro Max downscaled
ROT_TO_PORTRAIT = {1: 0, 3: 2, 6: 1, 8: 3}  # np.rot90 k turning the upright image into the buffer


def packet(ptype, ts_us, payload=b"", flags=0):
    return HEADER.pack(ptype, flags, ts_us, len(payload)) + payload


def split_nals(data):
    """Yield NAL units (without start codes) from Annex-B bytes."""
    i, n, starts = 0, len(data), []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0 and (data[i + 2] == 1 or (data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1)):
            sc = 3 if data[i + 2] == 1 else 4
            starts.append((i, i + sc))
            i += sc
        else:
            i += 1
    for idx, (s, body) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        yield data[body:end]


def make_upright_image(t, upright_w, upright_h):
    img = np.full((upright_h, upright_w, 3), 40, np.uint8)
    img[:, :, 2] = 90                                   # dark blue background
    img[:40, :] = (220, 30, 30)                          # red = TOP
    img[:, :40] = (30, 200, 30)                          # green = LEFT
    # "UP" arrow block in the middle
    cx, cy = upright_w // 2, upright_h // 2
    for row in range(120):
        half = row // 2
        img[cy - 120 + row, cx - half:cx + half] = (255, 255, 0)
    img[cy:cy + 120, cx - 25:cx + 25] = (255, 255, 0)
    # moving white box
    x = int((t * 300) % (upright_w - 100))
    img[upright_h - 160:upright_h - 60, x:x + 100] = 255
    return img


def serve(args):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    print(f"fake iPhone listening on 127.0.0.1:{args.port} (orientation {args.orient})")
    while True:
        conn, addr = srv.accept()
        print("receiver connected", addr)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            stream(conn, args)
        except (ConnectionError, OSError) as e:
            print("receiver gone:", e)
        finally:
            conn.close()


def stream(conn, args):
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = W, H, "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.framerate = Fraction(FPS, 1)
    enc.bit_rate = 8_000_000
    enc.options = {"preset": "ultrafast", "tune": "zerolatency", "bframes": "0",
                   "g": str(FPS), "profile": "main"}

    k = ROT_TO_PORTRAIT[args.orient]
    upright_w, upright_h = (H, W) if k % 2 else (W, H)
    t0 = time.perf_counter()
    conn.sendall(packet(T_ORIENTATION, 0, bytes([args.orient])))

    frame_i, last_hb, audio_phase, audio_sent_until = 0, 0.0, 0.0, t0
    while True:
        now = time.perf_counter()
        t = now - t0
        ts = int(t * 1e6)

        img = np.ascontiguousarray(np.rot90(make_upright_image(t, upright_w, upright_h), k))
        vf = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        vf.pts = frame_i
        frame_i += 1
        for p in enc.encode(vf):
            data = bytes(p)
            if p.is_keyframe:
                ps = b"".join(b"\x00\x00\x00\x01" + nal for nal in split_nals(data) if nal and nal[0] & 0x1F in (7, 8))
                conn.sendall(packet(T_VIDEO_CONFIG, ts, struct.pack(">HH", W, H) + ps))
            conn.sendall(packet(T_VIDEO_FRAME, ts, data, flags=1 if p.is_keyframe else 0))

        if not args.no_audio:
            # send audio up to "now" in 10 ms chunks of 480 frames
            while audio_sent_until < now:
                n = 480
                tt = (audio_phase + np.arange(n)) / 48000.0
                s = (0.2 * 32767 * np.sin(2 * math.pi * 440 * tt)).astype("<i2")
                stereo = np.repeat(s[:, None], 2, axis=1)
                conn.sendall(packet(T_AUDIO_PCM, int((audio_sent_until - t0) * 1e6), stereo.tobytes()))
                audio_phase += n
                audio_sent_until += n / 48000.0

        if now - last_hb >= 1.0:
            conn.sendall(packet(T_HEARTBEAT, 0))
            last_hb = now

        sleep = (frame_i / FPS) - (time.perf_counter() - t0)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=2345)
    ap.add_argument("--orient", type=int, default=6, choices=[1, 3, 6, 8])
    ap.add_argument("--no-audio", action="store_true")
    serve(ap.parse_args())
