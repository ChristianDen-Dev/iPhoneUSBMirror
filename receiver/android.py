"""
android.py - Android phones (USB or Wi-Fi) through adb + the scrcpy server.

We do NOT run scrcpy.exe. We only use two files from the official scrcpy
release (Apache 2.0, see vendor/android/LICENSE.txt):
  adb.exe (+ 2 DLLs)  Google's Android Debug Bridge - talks to the phone
  scrcpy-server       a small Java program we start ON the phone; it sends
                      the screen (H.264) and the sound (raw PCM) back to us

Everything else (window, GPU decoding, sound routing to OBS, recording...)
is our own code, the same as for the iPhone.

Protocol of scrcpy-server v5.0.1 (see its doc/develop.md):
  3 TCP sockets through "adb forward", opened in this order:
    video   : dummy byte, 64-byte device name, codec id u32,
              then 12-byte headers:
                session packet  (MSB of byte 0 = 1): width u32, height u32
                media packet    (MSB = 0): u64 [config bit 62 | key bit 61 | PTS],
                                u32 size, payload
    audio   : codec id u32 ("raw" = 0x00726177, 0 = disabled e.g. Android 10)
              then media packets (raw = 48 kHz s16le stereo)
    control : we send touch / key messages; the phone may send clipboard
              messages (we read and ignore them)

The phone needs: Developer options -> USB debugging ON, and "Allow" tapped
once on the phone for this computer.

Den-Dev Live app on the phone (preferred, same as the iPhone):
  When the app's mirror is on, it listens on phone ports 2345 / 2346 with the
  exact iPhone wire format. Over USB we reach it with "adb forward" (like
  usbmux for the iPhone). The app keeps capturing when the USB link blinks,
  so the laptop just re-forwards and is back in under a second. scrcpy is
  only the fallback (app not started) and a small helper for mouse control.
"""

import os
import random
import re
import socket
import struct
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor", "android")
ADB = os.path.join(VENDOR, "adb.exe")
SERVER_FILE = os.path.join(VENDOR, "scrcpy-server")
SERVER_VERSION = "5.0.1"            # must match the scrcpy-server file exactly
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
WIFI_PORT = 5555                    # adb over Wi-Fi ("adb tcpip 5555")

USB_BITRATE = 16_000_000
WIFI_BITRATE = 8_000_000            # Wi-Fi: less data = fewer stutters
MAX_SIZE = 1920                     # long side; same as the iPhone stream
MAX_FPS = 60

CODEC_H264 = 0x68323634
CODEC_RAW = 0x00726177
FLAG_CONFIG = 1 << 62
FLAG_KEY = 1 << 61
PTS_MASK = FLAG_KEY - 1

NO_WINDOW = 0x08000000              # CREATE_NO_WINDOW: no console flashes
# adb's own network discovery (mDNS) makes Windows Firewall ask about adb;
# we don't need it (Wi-Fi phones are reached with "adb connect IP").
ENV = dict(os.environ, ADB_MDNS="0", ADB_MDNS_AUTO_CONNECT="0")

# Android key codes (android.view.KeyEvent)
KEY_HOME, KEY_BACK, KEY_APP_SWITCH = 3, 4, 187
KEY_VOLUME_UP, KEY_VOLUME_DOWN, KEY_POWER = 24, 25, 26

# touch actions (android.view.MotionEvent)
TOUCH_DOWN, TOUCH_UP, TOUCH_MOVE = 0, 1, 2
POINTER_FINGER = (1 << 64) - 2      # SC_POINTER_ID_GENERIC_FINGER (-2 as u64)


def available():
    return os.path.isfile(ADB) and os.path.isfile(SERVER_FILE)


def adb(*args, serial=None, timeout=10):
    """Run one adb command, return (exit code, text output)."""
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as e:
        return -1, str(e)


def list_devices():
    """[(serial, state)] - state is "device" (ready), "unauthorized"
    (tap Allow on the phone) or "offline"."""
    code, out = adb("devices", timeout=8)
    if code != 0:
        return []
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("*"):
            found.append((parts[0], parts[1]))
    return found


def is_wifi_serial(serial):
    return bool(re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial)) or "._adb-tls-connect." in serial


def wifi_ip(serial):
    """The phone's Wi-Fi IPv4 (e.g. "192.168.1.23") or None."""
    for args in (("shell", "ip", "-o", "-4", "addr", "show", "wlan0"), ("shell", "ip", "-o", "-4", "addr")):
        code, out = adb(*args, serial=serial)
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", out):
            ip = m.group(1)
            if not ip.startswith("127."):
                return ip
    return None


def enable_wifi(serial):
    """USB-connected phone -> also reachable over Wi-Fi (adb tcpip).
    Returns (ip, message). Stays on until the phone restarts."""
    ip = wifi_ip(serial)
    if not ip:
        return None, "The phone is not on Wi-Fi"
    code, out = adb("tcpip", str(WIFI_PORT), serial=serial)
    if code != 0:
        return None, f"adb tcpip failed: {out}"
    time.sleep(2.0)
    code, out = adb("connect", f"{ip}:{WIFI_PORT}", timeout=12)
    ok = "connected" in out and "cannot" not in out
    return (ip if ok else None), out


def connect_wifi(host):
    """Try `adb connect host:5555` (phone was switched to Wi-Fi before)."""
    code, out = adb("connect", f"{host}:{WIFI_PORT}", timeout=6)
    return "connected" in out and "cannot" not in out and "failed" not in out


def forward(serial, remote_port):
    """adb forward tcp:<free> tcp:<remote_port> -> local port, or None."""
    code, out = adb("forward", "tcp:0", f"tcp:{remote_port}", serial=serial, timeout=5)
    last = out.strip().splitlines()[-1] if out.strip() else ""
    return int(last) if code == 0 and last.isdigit() else None


def remove_forward(serial, local_port):
    if local_port:
        adb("forward", "--remove", f"tcp:{local_port}", serial=serial, timeout=5)


def connect_app(serial, main_port=2345, audio_port=2346):
    """Den-Dev Live app mirror over adb. Returns (socket, main_local, audio_local)
    or None when the app's mirror is not on. The app sends ORIENTATION + INFO
    right away, so a real answer arrives within ~1 s (an adb forward to a closed
    port accepts and then closes at once)."""
    lp = forward(serial, main_port)
    if not lp:
        return None
    s = None
    try:
        s = socket.create_connection(("127.0.0.1", lp), timeout=2)
        s.settimeout(1.5)
        if len(s.recv(1, socket.MSG_PEEK)) == 1:
            s.settimeout(None)
            ap = forward(serial, audio_port)
            return s, lp, ap
    except OSError:
        pass
    if s:
        s.close()
    remove_forward(serial, lp)
    return None


def restart_adb():
    """Fresh adb server (after Windows re-plugs the phone, adb can lose it)."""
    adb("kill-server", timeout=8)
    adb("start-server", timeout=15)


def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("Android phone closed the connection")
        got += r
    return buf


class AndroidControl:
    """Mouse -> finger touches, and the Back / Home / Recent apps keys."""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        self.screen = (0, 0)       # current video size (the phone checks it)
        threading.Thread(target=self._drain, daemon=True, name="android-ctl-rx").start()

    def _send(self, data):
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def touch(self, action, x, y):
        w, h = self.screen
        if not w or not h:
            return
        x = max(0, min(w - 1, int(x)))
        y = max(0, min(h - 1, int(y)))
        pressure = 0xFFFF if action != TOUCH_UP else 0
        self._send(struct.pack(">BBQiiHHHII", 2, action, POINTER_FINGER, x, y, w, h, pressure, 0, 0))

    def key(self, keycode):
        for action in (0, 1):      # down, up
            self._send(struct.pack(">BBIII", 0, action, keycode, 0, 0))

    def _drain(self):
        """The phone may send clipboard messages; read and ignore them."""
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass


class AndroidSession:
    """One mirroring session with one Android phone."""

    def __init__(self, serial, keep_phone_sound=False, control_only=False):
        self.serial = serial
        self.control_only = control_only     # mouse helper next to the app: tiny video, no sound
        self.wifi = is_wifi_serial(serial)
        self.keep_phone_sound = keep_phone_sound
        self.proc = None
        self.socks = []
        self.video = self.audio = self.control_sock = None
        self.name = serial
        self.sdk = 0
        self.audio_ok = False
        self.log_tail = []

    # ---- start ---------------------------------------------------------------
    def start(self):
        code, out = adb("shell", "getprop", "ro.build.version.sdk", serial=self.serial)
        self.sdk = int(out) if out.strip().isdigit() else 0
        code, out = adb("push", SERVER_FILE, DEVICE_SERVER_PATH, serial=self.serial, timeout=30)
        if code != 0:
            raise RuntimeError(f"could not copy the server to the phone: {out}")

        scid = random.randint(0, 0x7FFFFFFF)
        code, out = adb("forward", "tcp:0", f"localabstract:scrcpy_{scid:08x}", serial=self.serial)
        if code != 0 or not out.strip().splitlines()[-1].isdigit():
            raise RuntimeError(f"adb forward failed: {out}")
        self.port = int(out.strip().splitlines()[-1])

        # "keep sound on the phone" needs Android 13+ (playback capture + duplicate)
        dup = self.keep_phone_sound and self.sdk >= 33
        if self.control_only:
            # The picture is only used for touch positions: 5 fps, small, cheap.
            args = [f"scid={scid:08x}", "log_level=warn", "video_codec=h264", "video_bit_rate=300000",
                    "max_size=800", "max_fps=5", "audio=false",
                    "tunnel_forward=true", "control=true", "clipboard_autosync=false"]
        else:
            args = [
                f"scid={scid:08x}", "log_level=info",
                "video_codec=h264", f"video_bit_rate={WIFI_BITRATE if self.wifi else USB_BITRATE}",
                f"max_size={MAX_SIZE}", f"max_fps={MAX_FPS}",
                "audio_codec=raw", f"audio_source={'playback' if dup else 'output'}",
                "tunnel_forward=true", "control=true", "clipboard_autosync=false",
            ]
            if dup:
                args.append("audio_dup=true")
        cmd = [ADB, "-s", self.serial, "shell", f"CLASSPATH={DEVICE_SERVER_PATH}", "app_process", "/",
               "com.genymobile.scrcpy.Server", SERVER_VERSION] + args
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV,
                                     creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._log_server, daemon=True, name="android-log").start()

        try:
            self.video = self._connect_first()
            self.audio = None if self.control_only else self._connect()
            self.control_sock = self._connect()
            self.control_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            name = bytes(recv_exact(self.video, 64)).split(b"\0", 1)[0]
            self.name = name.decode("utf-8", "replace") or self.serial
            vcodec = struct.unpack(">I", recv_exact(self.video, 4))[0]
            if vcodec != CODEC_H264:
                raise RuntimeError(f"unexpected video codec 0x{vcodec:08x}")
            if self.audio is not None:
                acodec = struct.unpack(">I", recv_exact(self.audio, 4))[0]
                self.audio_ok = acodec == CODEC_RAW
                if not self.audio_ok:
                    why = "needs Android 11 or newer" if acodec == 0 else f"error code {acodec}"
                    print(f"[android] no phone sound ({why})", flush=True)
        finally:
            adb("forward", "--remove", f"tcp:{self.port}", serial=self.serial)
        for s in (self.video, self.audio):
            if s is not None:
                s.settimeout(None)   # a closed USB / adb link ends recv() with an error
        self.control = AndroidControl(self.control_sock)
        if self.control_only:
            threading.Thread(target=self._drain_video, daemon=True, name="android-ctl-video").start()
        return self

    def _drain_video(self):
        """Mouse helper: read (and drop) the tiny picture, keep the touch size current."""
        try:
            for item in self.read_video():
                if item[0] == "size":
                    self.control.screen = item[1]
        except Exception:
            pass
        self.alive = False

    alive = True

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.socks.append(s)
        return s

    def _connect_first(self):
        """The forward accepts even before the server listens, so we wait for
        the server's dummy byte (up to ~10 s, like scrcpy)."""
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError("the server stopped on the phone: " + " | ".join(self.log_tail[-3:]))
            s = None
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                s.settimeout(2)
                if s.recv(1):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                    s.settimeout(None)
                    self.socks.append(s)
                    return s
            except OSError:
                pass
            if s:
                s.close()
            time.sleep(0.1)
        raise RuntimeError("the phone did not answer (server did not start)")

    def _log_server(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.log_tail = (self.log_tail + [line])[-10:]
                print(f"[android{'-mouse' if self.control_only else ''}] {line}", flush=True)

    # ---- streams -------------------------------------------------------------
    def read_video(self):
        """Yields ("size", (w, h)) or ("config"/"frame", is_key, pts_us, data)."""
        hdr = struct.Struct(">QI")
        while True:
            head = bytes(recv_exact(self.video, 12))
            if head[0] & 0x80:                                   # session packet (start / rotation)
                w, h = struct.unpack(">II", head[4:12])
                yield ("size", (w, h))
                continue
            pts_flags, size = hdr.unpack(head)
            data = bytes(recv_exact(self.video, size))
            if pts_flags & FLAG_CONFIG:
                yield ("config", False, 0, data)
            else:
                yield ("frame", bool(pts_flags & FLAG_KEY), pts_flags & PTS_MASK, data)

    def read_audio(self):
        """Yields (pts_us, pcm bytes) - 48 kHz s16le stereo."""
        hdr = struct.Struct(">QI")
        while True:
            pts_flags, size = hdr.unpack(bytes(recv_exact(self.audio, 12)))
            data = bytes(recv_exact(self.audio, size))
            if not pts_flags & FLAG_CONFIG:
                yield pts_flags & PTS_MASK, data

    def stop(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
