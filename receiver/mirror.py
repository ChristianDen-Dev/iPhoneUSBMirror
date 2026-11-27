"""
mirror.py - iPhone USB Mirror receiver (Windows). Run this file.

Shows the iPhone screen (sent over the USB cable by the iPhone app's
broadcast) in a window called "iPhone USB Mirror" and plays the game sound.

Three window modes:
  Normal      : proper window with a toolbar (buttons), live status bar and
                a step-by-step "how to connect" screen.
  Stream Mode : (F2) clean picture only, at the iPhone's exact pixel size
                (1:1, sharpest). Capture THIS in OBS / TikTok LIVE Studio.
                Right-click opens a Windows menu that never shows in the capture.
  Fullscreen  : (F11 or double-click) controls appear when the mouse moves.

Keys: F2 Stream Mode | F11 fullscreen | Esc back | F12 screenshot | F9 record
      M mute | Up/Down or mouse wheel volume | T always on top | R flip
      S sharpen | F1 statistics | O open recordings folder | H help

Debug: --tcp 127.0.0.1:2345 connects to a plain TCP address instead of usbmux
       (used with tools\\fake_iphone.py for testing without the phone).
"""

import argparse
import collections
import json
import math
import os
import time

import pygame

import ui
import winutil
from audio_player import AudioRouter
from core import ST_CONNECTED, ST_NO_APP, ST_NO_USB, Receiver, Shared
from recorder import Recorder
from ui import (ACCENT, AMBER, BG, DIM, FAINT, GREEN, HEAT_COLORS, HOVER, LINE, ON, PANEL,
                PANEL_2, PRESSED, RED, TEXT)

WINDOW_TITLE = "iPhone USB Mirror"
APP_FOLDER = "iPhone USB Mirror"
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    "flip_landscape": False,
    "sharpen": True,
    "stream_mode": False,
    "stream_hide_titlebar": False,   # Stream Mode without a title bar (old behaviour)
    "always_on_top": False,
    "show_stats": False,
    "volume": 100,                   # laptop volume 0-150 % (the stream always gets 100 %)
    "stream_output": True,           # send game sound to the VB-Audio cable for TikTok / OBS
    "mic_to_stream": True,           # iPhone mic (if switched on in the iPhone sheet) -> stream
    "mic_gain": 100,                 # iPhone mic loudness in the stream, %
    "window_w": 1280,
    "window_h": 800,
}

HELP_ROWS = [
    ("F2", "Stream Mode: clean 1:1 picture for OBS / TikTok LIVE Studio"),
    ("F11 / double-click", "Fullscreen"),
    ("Esc", "Leave fullscreen / Stream Mode / this help"),
    ("F12", "Screenshot  (Pictures \\ iPhone USB Mirror)"),
    ("F9", "Start / stop recording  (Videos \\ iPhone USB Mirror)"),
    ("M", "Laptop sound on/off (the stream keeps its sound via VB-Cable)"),
    ("Up / Down, wheel", "Laptop volume"),
    ("A", "Sound panel: where game sound and mic come from"),
    ("N", "Mute / unmute the iPhone mic in the stream"),
    ("T", "Always on top"),
    ("R", "Flip landscape 180°"),
    ("S", "Sharpen when the picture is enlarged"),
    ("F1", "Statistics"),
    ("O", "Open recordings folder"),
    ("Right-click", "All options"),
]


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in cfg and type(v) is type(cfg[k]):
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def fix_default_speaker():
    """If Windows made the virtual cable the default speaker (VB-Cable's
    installer does that), switch the default back to the real speakers -
    otherwise every other laptop sound would go nowhere."""
    try:
        from audio_player import wasapi_outputs
        devs, default = wasapi_outputs()
        names = dict(devs)
        if "CABLE" not in names.get(default, ""):
            return
        real = next((n for _, n in devs if "CABLE" not in n and "VB-Audio" not in n), None)
        if not real:
            return
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "set_default_speaker.ps1")
        match = real.split("(")[-1].split(")")[0] if "(" in real else real    # e.g. "Realtek(R) Audio"
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, match],
                           capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        print(f"[audio] Windows default speaker was the cable -> {r.stdout.strip().splitlines()[-1:]}", flush=True)
        import sounddevice as sd
        sd._terminate()          # re-read the device list (new default)
        sd._initialize()
    except Exception as e:
        print(f"[audio] could not fix the default speaker: {e}", flush=True)


def fmt_time(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class App:
    def __init__(self, tcp_address):
        winutil.set_dpi_aware()
        self.cfg = load_settings()

        s = self.s = Shared()
        s.flip_landscape = self.cfg["flip_landscape"]
        s.sharpen = self.cfg["sharpen"]
        fix_default_speaker()
        self.router = s.audio = AudioRouter(self.cfg["stream_output"])
        self.audio = self.router.laptop          # what you hear on the laptop
        self.router.mic_to_stream = self.cfg["mic_to_stream"]
        self.router.stream.mix_gain = self.cfg["mic_gain"] / 100
        self.audio.set_volume(self.cfg["volume"] / 100)
        print(f"[audio] stream output: {self.router.cable_name or 'VB-Audio cable not found'}"
              f"{'' if self.cfg['stream_output'] else ' (switched off)'}", flush=True)
        self.rec = s.recorder = Recorder()
        self.rec.on_finished = self._recording_finished

        pygame.init()
        pygame.display.set_caption(WINDOW_TITLE)
        pygame.display.set_icon(ui.make_window_icon())
        self.p = ui.Painter(winutil.ui_scale())
        self.desktop = pygame.display.get_desktop_sizes()[0]

        self.fullscreen = False
        self.stream_mode = self.cfg["stream_mode"]
        self.windowed_size = (self.cfg["window_w"], self.cfg["window_h"])
        self.normal_pos = None
        self.stream_size = None
        self.screen = None
        self.set_window()

        # UI state
        self.buttons = []                # [(key, rect, tooltip)] from the last draw
        self.slider_rect = None
        self.meter_rect = None
        self.hover, self.hover_since, self.tip_shown = None, 0.0, False
        self.pressed = None
        self.dragging = False
        self.show_help = False
        self.show_sound = False
        self.last_mouse = time.monotonic()
        self.cursor_visible = True
        self.overlay_visible = False     # fullscreen toolbar
        self.last_click = (0.0, (0, 0))
        self.toasts = []                 # [(text, color, start)]
        self.messages = collections.deque()   # toasts from other threads
        self.current = None              # (surface, numpy array kept alive)
        self.last_seq = -1
        self.dirty = True
        self.last_sig = None
        # stats
        self.fps = 0.0
        self.mbps = 0.0
        self._tick_t = time.perf_counter()
        self._tick_frames = 0
        self._tick_bytes = 0
        self._shown = 0
        self._log_tick = 0

        self.receiver = Receiver(s, tcp_address)
        self.receiver.start()

    # ================================================================ window
    @property
    def mode(self):
        return "full" if self.fullscreen else ("stream" if self.stream_mode else "normal")

    @property
    def live(self):
        return self.current is not None and self.s.state == ST_CONNECTED

    def hwnd(self):
        return winutil.get_hwnd()

    def stream_window_size(self):
        vw, vh = self.s.video_size
        if vw <= 0 or vh <= 0:
            vw, vh = 1918, 886                       # landscape iPhone until the first frame
        wa = winutil.work_area() or (0, 0, *self.desktop)
        ew, eh = (0, 0) if self.cfg["stream_hide_titlebar"] else winutil.frame_extra(self.hwnd())
        avail_w = wa[2] - wa[0] - ew
        avail_h = wa[3] - wa[1] - eh
        scale = min(1.0, avail_w / vw, avail_h / vh)
        return (max(2, int(vw * scale) & ~1), max(2, int(vh * scale) & ~1))

    def set_window(self):
        mode = self.mode
        if mode == "full":
            self.screen = pygame.display.set_mode(self.desktop, pygame.NOFRAME)
            winutil.move_window(self.hwnd(), 0, 0)
        elif mode == "stream":
            size = self.stream_window_size()
            flags = pygame.NOFRAME if self.cfg["stream_hide_titlebar"] else 0
            self.screen = pygame.display.set_mode(size, flags)
            self.stream_size = size
        else:
            self.screen = pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE)
        hwnd = self.hwnd()
        winutil.dark_title_bar(hwnd)
        winutil.set_topmost(hwnd, self.cfg["always_on_top"])
        if mode != "full":
            winutil.keep_on_screen(hwnd)
        self.dirty = True

    def remember_normal_pos(self):
        if self.mode == "normal":
            self.windowed_size = self.screen.get_size()
            r = winutil.window_rect(self.hwnd())
            self.normal_pos = r[:2] if r else None

    def restore_normal_pos(self):
        if self.mode == "normal" and self.normal_pos:
            winutil.move_window(self.hwnd(), *self.normal_pos)
            winutil.keep_on_screen(self.hwnd())

    def save(self):
        self.cfg["flip_landscape"] = self.s.flip_landscape
        self.cfg["sharpen"] = self.s.sharpen
        self.cfg["stream_mode"] = self.stream_mode
        self.cfg["window_w"], self.cfg["window_h"] = self.windowed_size
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=1)
        except OSError:
            pass

    # ================================================================ actions
    def toast(self, text, color=TEXT, force=False):
        print(f"[ui] {text}", flush=True)
        if self.mode != "stream" or force:          # keep the stream picture clean
            self.toasts = [(text, color, time.monotonic())]
            self.dirty = True

    def do(self, key):
        s = self.s
        if key == "stream":
            self.remember_normal_pos()
            self.fullscreen = False
            self.stream_mode = not self.stream_mode
            self.set_window()
            self.restore_normal_pos()
            self.save()
            if self.stream_mode:
                self.toast("Stream Mode  ·  Esc or F2 to exit  ·  right-click for options", force=True)
        elif key == "full":
            if not self.fullscreen:
                self.remember_normal_pos()
            self.fullscreen = not self.fullscreen
            self.set_window()
            self.restore_normal_pos()
        elif key == "shot":
            if not self.live:
                self.toast("No picture yet - start the broadcast first", AMBER)
            else:
                name = time.strftime("Screenshot %Y-%m-%d %H-%M-%S.png")
                path = os.path.join(winutil.known_folder(winutil.FOLDER_PICTURES, "Pictures"), APP_FOLDER, name)
                s.snapshot_request = (path, self._screenshot_done)
        elif key == "rec":
            if self.rec.active:
                self.rec.stop()
                self.toast("Saving recording...", DIM)
            elif not self.live:
                self.toast("No picture yet - start the broadcast first", AMBER)
            else:
                name = time.strftime("Recording %Y-%m-%d %H-%M-%S.mp4")
                path = os.path.join(winutil.known_folder(winutil.FOLDER_VIDEOS, "Videos"), APP_FOLDER, name)
                self.rec.start(path)
                self.toast("Recording started  (F9 to stop)", RED)
        elif key == "folder":
            winutil.open_folder(os.path.join(winutil.known_folder(winutil.FOLDER_VIDEOS, "Videos"), APP_FOLDER))
        elif key == "flip":
            s.flip_landscape = not s.flip_landscape
            self.save()
            self.toast(f"Landscape flip {'on' if s.flip_landscape else 'off'}")
        elif key == "sharpen":
            s.sharpen = not s.sharpen
            self.save()
            self.toast(f"Sharpen {'on' if s.sharpen else 'off'}")
        elif key == "pin":
            self.cfg["always_on_top"] = not self.cfg["always_on_top"]
            winutil.set_topmost(self.hwnd(), self.cfg["always_on_top"])
            self.save()
            self.toast(f"Always on top {'on' if self.cfg['always_on_top'] else 'off'}")
        elif key == "mute":
            muted = self.audio.toggle_mute()
            if not muted:
                self.toast("Laptop sound on")
            elif self.router.cable_ok:
                self.toast("Laptop sound off  -  the stream still has the game sound", GREEN)
            else:
                self.toast("Sound off - this also mutes the stream (VB-Cable output is off)", AMBER)
        elif key == "micmute":
            if not self.router.mic_active:
                self.toast("iPhone mic is off - turn it on in the iPhone Start Broadcast sheet", AMBER)
            else:
                self.router.mic_muted = not self.router.mic_muted
                if self.router.mic_muted:
                    self.router.stream.reset_mix()     # drop mic sound still waiting
                self.toast("iPhone mic MUTED" if self.router.mic_muted else "iPhone mic on (stream)",
                           RED if self.router.mic_muted else GREEN, force=True)
        elif key == "micstream":
            self.cfg["mic_to_stream"] = not self.cfg["mic_to_stream"]
            self.router.mic_to_stream = self.cfg["mic_to_stream"]
            self.save()
            self.toast("iPhone mic in the stream: " + ("ON" if self.cfg["mic_to_stream"] else "OFF"))
        elif key in ("mic+", "mic-"):
            self.cfg["mic_gain"] = int(min(300, max(0, self.cfg["mic_gain"] + (25 if key == "mic+" else -25))))
            self.router.stream.mix_gain = self.cfg["mic_gain"] / 100
            self.save()
            self.toast(f"iPhone mic loudness {self.cfg['mic_gain']} %")
        elif key == "sound":
            self.show_sound = not self.show_sound
            self.show_help = False
        elif key == "cable":
            if not self.router.cable_name:
                self.toast("VB-Audio Virtual Cable is not installed", AMBER)
            else:
                self.cfg["stream_output"] = not self.cfg["stream_output"]
                self.router.set_stream_enabled(self.cfg["stream_output"])
                self.save()
                self.toast("Game sound to stream cable: " + ("ON" if self.cfg["stream_output"] else "OFF"))
        elif key in ("vol+", "vol-"):
            self.set_volume(self.cfg["volume"] + (10 if key == "vol+" else -10))
        elif key == "stats":
            self.cfg["show_stats"] = not self.cfg["show_stats"]
            self.save()
        elif key == "help":
            self.show_help = not self.show_help
            self.show_sound = False
        elif key == "hidetitle":
            self.cfg["stream_hide_titlebar"] = not self.cfg["stream_hide_titlebar"]
            self.save()
            if self.mode == "stream":
                self.set_window()
        elif key == "menu":
            self.show_menu()
        elif key == "quit":
            s.running = False
        self.dirty = True

    def set_volume(self, v):
        v = int(min(150, max(0, v)))
        self.cfg["volume"] = v
        self.audio.set_volume(v / 100)
        if self.audio.muted and v > 0:
            self.audio.toggle_mute()
        self.dirty = True

    def show_menu(self):
        rec = self.rec.active
        items = [
            ("stream", "Stream Mode (clean picture for OBS / TikTok)\tF2", self.stream_mode, True),
            ("full", "Fullscreen\tF11", self.fullscreen, True),
            None,
            ("shot", "Take screenshot\tF12", False, self.live),
            ("rec", ("Stop recording" if rec else "Start recording") + "\tF9", rec, rec or self.live),
            ("folder", "Open recordings folder\tO", False, True),
            None,
            ("sound", "Sound panel (game sound / mic)\tA", self.show_sound, True),
            ("cable", "Send game sound to stream (VB-Cable)", self.router.cable_ok, bool(self.router.cable_name)),
            ("mute", "Laptop sound off\tM", self.audio.muted, True),
            ("micstream", "Use iPhone mic in the stream", self.cfg["mic_to_stream"], self.router.cable_ok),
            ("micmute", "Mute iPhone mic\tN", self.router.mic_muted, self.router.mic_active),
            ("mic+", f"iPhone mic louder  ({self.cfg['mic_gain']} %)", False, self.cfg["mic_gain"] < 300),
            ("mic-", "iPhone mic quieter", False, self.cfg["mic_gain"] > 0),
            ("vol+", f"Laptop volume up  ({self.cfg['volume']} %)\tUp", False, self.cfg["volume"] < 150),
            ("vol-", "Laptop volume down\tDown", False, self.cfg["volume"] > 0),
            None,
            ("pin", "Always on top\tT", self.cfg["always_on_top"], True),
            ("flip", "Flip landscape 180°\tR", self.s.flip_landscape, True),
            ("sharpen", "Sharpen when enlarged\tS", self.s.sharpen, True),
            ("hidetitle", "Stream Mode: hide title bar", self.cfg["stream_hide_titlebar"], True),
            ("stats", "Show statistics\tF1", self.cfg["show_stats"], True),
            None,
            ("help", "Keyboard shortcuts\tH", self.show_help, True),
            ("quit", "Exit\tAlt+F4", False, True),
        ]
        self.pressed, self.dragging = None, False
        key = winutil.popup_menu(self.hwnd(), items)
        if key:
            self.do(key)
        self.dirty = True

    def _screenshot_done(self, path, error):
        self.messages.append((f"Screenshot saved: {os.path.basename(path)}", GREEN) if not error
                             else (f"Screenshot failed: {error}", RED))

    def _recording_finished(self, path, error):
        if error:
            self.messages.append((f"Recording problem: {error}", RED))
        else:
            self.messages.append((f"Recording saved: {os.path.basename(path)}  (O opens the folder)", GREEN))

    # ================================================================ input
    KEYS = {
        pygame.K_F2: "stream", pygame.K_F11: "full", pygame.K_F12: "shot", pygame.K_F9: "rec",
        pygame.K_m: "mute", pygame.K_UP: "vol+", pygame.K_DOWN: "vol-", pygame.K_EQUALS: "vol+",
        pygame.K_PLUS: "vol+", pygame.K_KP_PLUS: "vol+", pygame.K_MINUS: "vol-", pygame.K_KP_MINUS: "vol-",
        pygame.K_t: "pin", pygame.K_r: "flip", pygame.K_s: "sharpen", pygame.K_F1: "stats",
        pygame.K_o: "folder", pygame.K_h: "help", pygame.K_a: "sound", pygame.K_n: "micmute",
    }

    def hit(self, pos):
        if self.slider_rect and self.slider_rect.collidepoint(pos):
            return "slider"
        if self.meter_rect and self.meter_rect.collidepoint(pos):
            return "sound"
        for key, rect, _ in self.buttons:
            if rect.collidepoint(pos):
                return key
        return None

    def slider_set(self, x):
        r = self.slider_rect
        frac = (x - r.left - self.p.px(6)) / max(1, r.width - self.p.px(12))
        self.set_volume(round(min(1.0, max(0.0, frac)) * 150 / 5) * 5)

    def handle_events(self):
        now = time.monotonic()
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                self.s.running = False
            elif e.type in (pygame.VIDEORESIZE, pygame.WINDOWSIZECHANGED):
                self.screen = pygame.display.get_surface()
                if self.mode == "normal":
                    self.windowed_size = self.screen.get_size()
                self.dirty = True
            elif e.type in (pygame.WINDOWEXPOSED, pygame.WINDOWFOCUSGAINED, pygame.WINDOWRESTORED):
                self.dirty = True
            elif e.type == pygame.MOUSEMOTION:
                self.last_mouse = now
                if not self.cursor_visible:
                    pygame.mouse.set_visible(True)
                    self.cursor_visible = True
                if self.dragging:
                    self.slider_set(e.pos[0])
                h = self.hit(e.pos)
                if h != self.hover:
                    self.hover, self.hover_since, self.tip_shown = h, now, False
                    self.dirty = True
            elif e.type == pygame.WINDOWLEAVE:
                if self.hover:
                    self.hover = None
                    self.dirty = True
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                h = self.hit(e.pos)
                if h == "slider":
                    self.dragging = True
                    self.slider_set(e.pos[0])
                elif h:
                    self.pressed = h
                    self.dirty = True
                elif self.mode != "stream":
                    t, p = self.last_click
                    if now - t < 0.35 and abs(e.pos[0] - p[0]) < 8 and abs(e.pos[1] - p[1]) < 8:
                        self.do("full")
                        self.last_click = (0.0, (0, 0))
                    else:
                        self.last_click = (now, e.pos)
            elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                if self.dragging:
                    self.dragging = False
                    self.save()
                elif self.pressed:
                    key, self.pressed = self.pressed, None
                    if self.hit(e.pos) == key:
                        self.do(key)
                    self.dirty = True
            elif e.type == pygame.MOUSEBUTTONUP and e.button == 3:
                self.show_menu()
            elif e.type == pygame.MOUSEWHEEL:
                self.set_volume(self.cfg["volume"] + 5 * (1 if e.y > 0 else -1))
                self.toast(f"Volume {self.cfg['volume']} %")
                self.save()
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    if self.show_help or self.show_sound:
                        self.show_help = self.show_sound = False
                    elif self.fullscreen:
                        self.do("full")
                    elif self.stream_mode:
                        self.do("stream")
                    self.dirty = True
                elif e.key in self.KEYS:
                    key = self.KEYS[e.key]
                    self.do(key)
                    if key in ("vol+", "vol-"):
                        self.toast(f"Volume {self.cfg['volume']} %")
                        self.save()

        # cursor + fullscreen toolbar auto-hide (never in normal mode)
        idle = now - self.last_mouse
        if self.mode != "normal" and idle > 2.0 and self.cursor_visible and not self.dragging:
            pygame.mouse.set_visible(False)
            self.cursor_visible = False
        overlay = self.mode == "full" and (idle < 2.5 or self.dragging)
        if overlay != self.overlay_visible:
            self.overlay_visible = overlay
            self.dirty = True
        if self.hover and not self.tip_shown and now - self.hover_since > 0.5:
            self.dirty = True

    # ================================================================ drawing
    def layout(self):
        w, h = self.screen.get_size()
        if self.mode == "normal":
            tb, sb = self.p.px(48), self.p.px(28)
            return (pygame.Rect(0, 0, w, tb), pygame.Rect(0, tb, w, max(2, h - tb - sb)),
                    pygame.Rect(0, h - sb, w, sb))
        toolbar = pygame.Rect(0, 0, w, self.p.px(48)) if self.overlay_visible else None
        return toolbar, pygame.Rect(0, 0, w, h), None

    def status_pill(self):
        st = self.s.state
        if self.live:
            vw, vh = self.s.video_size
            return f"Live  {vw}×{vh}  {self.fps:.0f} fps", GREEN
        if st == ST_CONNECTED:
            return "Connected - waiting for picture", ACCENT
        if st == ST_NO_APP:
            return "iPhone found - start the broadcast", ACCENT
        return "Waiting for iPhone (USB)", AMBER

    def toolbar_specs(self):
        a, s, c = self.audio, self.s, self.cfg
        rec = self.rec.active
        return [
            ("stream", "stream", "Stream Mode: clean picture for OBS / TikTok (F2)", self.stream_mode, None),
            ("full", "unfull" if self.fullscreen else "full", "Fullscreen (F11)", self.fullscreen, None),
            "sep",
            ("shot", "shot", "Screenshot (F12)", False, None),
            ("rec", "stop" if rec else "rec", "Stop recording (F9)" if rec else "Record video (F9)", rec, RED),
            "sep",
            ("flip", "flip", "Flip landscape 180° (R)", s.flip_landscape, None),
            ("sharpen", "HD", "Sharpen when enlarged (S)", s.sharpen, None),
            ("pin", "pin" if not c["always_on_top"] else "unpin", "Always on top (T)", c["always_on_top"], None),
            "sep",
            "meter",
            *([("micmute", "micoff" if self.router.mic_muted else "mic",
                "iPhone mic in the stream: on/off (N)", self.router.mic_muted, RED if self.router.mic_muted else None)]
              if self.router.mic_active else []),
            ("mute", "mute" if a.muted else "vol", "Laptop sound on/off - stream keeps sound (M)", a.muted, None),
            "slider",
            "sep",
            ("stats", "stats", "Statistics (F1)", c["show_stats"], None),
            ("folder", "folder", "Open recordings folder (O)", False, None),
            ("help", "help", "Keyboard shortcuts (H)", self.show_help, None),
            ("menu", "menu", "All options (or right-click)", False, None),
        ]

    def draw_toolbar(self, rect):
        p, scr = self.p, self.screen
        pygame.draw.rect(scr, ui.PANEL, rect)
        pygame.draw.line(scr, LINE, (rect.left, rect.bottom - 1), (rect.right, rect.bottom - 1))
        cy = rect.centery
        bw, bh, gap, sep, sl_w = p.px(36), p.px(34), p.px(2), p.px(14), p.px(118)
        specs = self.toolbar_specs()
        mt_w = p.px(30)
        total = sum(sep if x == "sep" else sl_w if x == "slider" else mt_w if x == "meter" else bw + gap
                    for x in specs)
        x = rect.right - total - p.px(8)

        # left: app name + status pills (only if there is room)
        lx = p.px(12)
        icon = pygame.transform.smoothscale(ui.make_window_icon(), (p.px(22), p.px(22)))
        title = p.text(p.f_ui_b, WINDOW_TITLE)
        label, color = self.status_pill()
        need = icon.get_width() + p.px(8) + title.get_width() + p.px(14) + p.f_small.size(label)[0] + p.px(40)
        if lx + need < x:
            scr.blit(icon, (lx, cy - icon.get_height() // 2))
            lx += icon.get_width() + p.px(8)
            scr.blit(title, (lx, cy - title.get_height() // 2))
            lx += title.get_width() + p.px(14)
        if lx + p.f_small.size(label)[0] + p.px(40) < x:
            lx += p.pill(scr, lx, cy, label, color) + p.px(8)
        if self.rec.active:
            rl = f"REC {fmt_time(self.rec.elapsed)}"
            if lx + p.f_small.size(rl)[0] + p.px(40) < x:
                p.pill(scr, lx, cy, rl, RED)

        # right: buttons
        self.buttons, self.slider_rect, self.meter_rect = [], None, None
        for spec in specs:
            if spec == "meter":
                self.draw_meter(pygame.Rect(x, cy - bh // 2, mt_w, bh))
                x += mt_w
                continue
            if spec == "sep":
                pygame.draw.line(scr, LINE, (x + sep // 2, cy - p.px(12)), (x + sep // 2, cy + p.px(12)))
                x += sep
                continue
            if spec == "slider":
                self.draw_slider(pygame.Rect(x, cy - bh // 2, sl_w, bh))
                x += sl_w
                continue
            key, icon_name, tip, toggled, color = spec
            r = pygame.Rect(x, cy - bh // 2, bw, bh)
            bg = None
            if self.pressed == key:
                bg = PRESSED
            elif self.hover == key:
                bg = HOVER
            elif toggled:
                bg = (96, 28, 28) if key == "rec" else ON
            if bg:
                pygame.draw.rect(scr, bg, r, border_radius=p.px(6))
            if icon_name == "HD":
                surf = p.text(p.f_hd, "HD", TEXT if toggled else DIM)
            else:
                surf = p.icon(icon_name, color or (TEXT if toggled or self.hover == key else (205, 208, 216)))
            p.blit_center(scr, surf, r)
            self.buttons.append((key, r, tip))
            x += bw + gap

    def draw_slider(self, r):
        p, scr = self.p, self.screen
        self.slider_rect = r
        vol = self.cfg["volume"]
        track = pygame.Rect(r.left + p.px(6), r.centery - p.px(2), r.width - p.px(52), p.px(4))
        pygame.draw.rect(scr, PANEL_2, track, border_radius=p.px(2))
        frac = vol / 150
        fill = track.copy()
        fill.width = int(track.width * frac)
        col = FAINT if self.audio.muted else (AMBER if vol > 100 else ACCENT)
        pygame.draw.rect(scr, col, fill, border_radius=p.px(2))
        mark = track.left + int(track.width * 100 / 150)          # 100 % mark
        pygame.draw.line(scr, FAINT, (mark, track.top - p.px(3)), (mark, track.bottom + p.px(2)))
        knob = (track.left + int(track.width * frac), track.centery)
        pygame.draw.circle(scr, TEXT if self.hover == "slider" or self.dragging else (210, 212, 220), knob, p.px(6))
        t = p.text(p.f_small, "mute" if self.audio.muted else f"{vol}%", DIM)
        scr.blit(t, (track.right + p.px(8), r.centery - t.get_height() // 2))

    def level_fraction(self):
        """Game sound level 0..1 on a dB scale (-50 dB .. 0 dB)."""
        lv = self.router.level if self.live else 0.0
        if lv <= 0.0:
            return 0.0
        return min(1.0, max(0.0, (20 * math.log10(lv) + 50) / 50))

    def draw_meter(self, r):
        """5 small bars showing the game sound level (click = sound panel)."""
        p, scr = self.p, self.screen
        self.meter_rect = r
        if self.hover == "sound" or self.show_sound:
            pygame.draw.rect(scr, ON if self.show_sound else HOVER, r, border_radius=p.px(6))
        frac = self.level_fraction()
        n, bw, gap = 5, p.px(3), p.px(2)
        x = r.centerx - (n * bw + (n - 1) * gap) // 2
        for i in range(n):
            h = p.px(5 + i * 3)
            lit = frac >= (i + 0.5) / n
            col = (RED if i == 4 else AMBER if i == 3 else GREEN) if lit else PANEL_2
            pygame.draw.rect(scr, col, (x, r.centery + p.px(8) - h, bw, h), border_radius=1)
            x += bw + gap

    def draw_sound_panel(self, area):
        p, scr, rt = self.p, self.screen, self.router
        w = min(area.width - p.px(20), p.px(640))
        h = min(area.height - p.px(20), p.px(360))
        box = pygame.Rect(0, 0, w, h)
        box.center = area.center
        p.panel(scr, box, PANEL, 14)
        x0, y = box.left + p.px(24), box.top + p.px(18)
        col2 = x0 + p.px(190)
        t = p.text(p.f_h1, "Sound", TEXT)
        scr.blit(t, (x0, y))
        y += t.get_height() + p.px(14)

        def row(title, value, color, note=""):
            nonlocal y
            scr.blit(p.text(p.f_ui_b, title, DIM), (x0, y))
            scr.blit(p.text(p.f_ui, value, color), (col2, y))
            y += p.px(24)
            if note:
                scr.blit(p.text(p.f_small, note, FAINT), (col2, y - p.px(2)))
                y += p.px(22)
            y += p.px(6)

        scr.blit(p.text(p.f_ui_b, "Game sound (iPhone)", DIM), (x0, y))
        bar = pygame.Rect(col2, y + p.px(6), box.right - col2 - p.px(28), p.px(10))
        pygame.draw.rect(scr, PANEL_2, bar, border_radius=p.px(5))
        fill = bar.copy()
        fill.width = int(bar.width * self.level_fraction())
        if fill.width > 0:
            pygame.draw.rect(scr, GREEN, fill, border_radius=p.px(5))
        y += p.px(34)

        if rt.cable_ok:
            row("To the stream", "VB-Audio cable - always on, even when the laptop is muted", GREEN,
                "In TikTok LIVE Studio / OBS:  Desktop audio = CABLE Input")
        elif rt.cable_name:
            row("To the stream", "Cable output switched OFF", AMBER, "Right-click \u2192 Send game sound to stream")
        else:
            row("To the stream", "via laptop speakers (Desktop audio)", AMBER,
                "Install VB-Audio Virtual Cable to stream with the laptop muted")
        laptop = "OFF (muted)" if self.audio.muted else f"ON  {self.cfg['volume']} %"
        row("Laptop speakers", laptop, AMBER if self.audio.muted else GREEN,
            (self.audio.device_name or "-") + "   \u00b7   M = on/off")
        if rt.mic_active and rt.cable_ok and rt.mic_to_stream:
            state = "MUTED (N to unmute)" if rt.mic_muted else f"ON - mixed into the stream  ({self.cfg['mic_gain']} %)"
            scr.blit(p.text(p.f_ui_b, "Microphone", DIM), (x0, y))
            scr.blit(p.text(p.f_ui, "iPhone mic  \u2192  " + state, RED if rt.mic_muted else GREEN), (col2, y))
            y += p.px(26)
            bar = pygame.Rect(col2, y, box.right - col2 - p.px(28), p.px(8))
            pygame.draw.rect(scr, PANEL_2, bar, border_radius=p.px(4))
            lv = 0.0 if rt.mic_muted or rt.mic_level <= 0 else min(1.0, max(0.0, (20 * math.log10(rt.mic_level) + 50) / 50))
            if lv > 0:
                fill = bar.copy()
                fill.width = int(bar.width * lv)
                pygame.draw.rect(scr, ACCENT, fill, border_radius=p.px(4))
            y += p.px(14)
            scr.blit(p.text(p.f_small, "Turn OFF the Mic in TikTok / OBS so your voice is not there twice.", FAINT),
                     (col2, y))
            y += p.px(28)
        elif rt.mic_active:
            row("Microphone", "iPhone mic is on, but not sent", AMBER,
                "Needs the VB-Audio cable + right-click \u2192 Use iPhone mic in the stream")
        else:
            row("Microphone", "laptop / headset mic in TikTok LIVE Studio / OBS", TEXT,
                "Want the iPhone mic? In the iPhone Start Broadcast sheet tap Microphone ON.")
        t = p.text(p.f_small, "Esc or A to close", FAINT)
        scr.blit(t, (box.right - t.get_width() - p.px(20), box.bottom - p.px(26)))

    def draw_statusbar(self, rect):
        p, scr, s = self.p, self.screen, self.s
        pygame.draw.rect(scr, ui.PANEL, rect)
        pygame.draw.line(scr, LINE, rect.topleft, (rect.right, rect.top))
        cy = rect.centery
        if self.live:
            vw, vh = s.video_size
            dec = "GPU decode" if s.decoder_name in ("cuda", "d3d11va") else "CPU decode"
            left = ["USB", s.codec or "H.264", f"{vw}×{vh}", f"{self.fps:.0f} fps", f"{self.mbps:.1f} Mbps", dec]
        else:
            left = ["Not mirroring"]
        x = p.px(12)
        for i, seg in enumerate(left):
            t = p.text(p.f_small, seg, DIM if i else TEXT)
            scr.blit(t, (x, cy - t.get_height() // 2))
            x += t.get_width() + p.px(16)

        right = []
        if self.rec.active:
            right.append((f"● REC {fmt_time(self.rec.elapsed)}  {self.rec.size_mb:.0f} MB", RED))
        if self.live:
            heat = s.thermal
            if heat:
                right.append((f"iPhone heat: {heat.capitalize()}", HEAT_COLORS.get(heat, DIM)))
            right.append((f"Audio {self.audio.buffered_ms:.0f} ms", DIM))
        if self.router.mic_active:
            if self.router.mic_muted or not self.router.mic_to_stream:
                right.append(("iPhone mic: MUTED" if self.router.mic_muted else "iPhone mic: not in stream", RED))
            else:
                right.append(("iPhone mic: on", GREEN))
        if self.router.cable_ok:
            right.append(("Stream sound: cable", GREEN))
        right.append(("Laptop sound off" if self.audio.muted else f"Laptop {self.cfg['volume']}%",
                      AMBER if self.audio.muted else DIM))
        x = rect.right - p.px(12)
        for text, col in reversed(right):
            t = p.text(p.f_small, text, col)
            x -= t.get_width()
            scr.blit(t, (x, cy - t.get_height() // 2))
            x -= p.px(18)

    def draw_waiting(self, area):
        p, scr, st = self.p, self.screen, self.s.state
        scr.fill(BG, area)
        steps = [
            ("Plug the iPhone into this laptop with the USB cable and unlock it", st != ST_NO_USB),
            ("In the iPhone USB Mirror app tap the round button → Start Broadcast", st == ST_CONNECTED),
            ("Open your game - the picture appears here", False),
        ]
        title = {ST_NO_USB: "Connect your iPhone", ST_NO_APP: "Start the broadcast"}.get(st, "Almost there...")
        cw = min(p.px(600), area.width - p.px(32))
        ch = p.px(300)
        card = pygame.Rect(0, 0, cw, ch)
        card.center = area.center
        if cw < p.px(260) or area.height < ch:          # too small: one line only
            t = p.text(p.f_ui, title, TEXT)
            p.blit_center(scr, t, area)
            return
        p.panel(scr, card, PANEL, 14)
        y = card.top + p.px(26)
        t = p.text(p.f_h1, title, TEXT)
        scr.blit(t, (card.left + p.px(28), y))
        y += t.get_height() + p.px(18)
        active_found = False
        for text, done in steps:
            active = not done and not active_found
            active_found = active_found or active
            cx = card.left + p.px(40)
            p.check_circle(scr, (cx, y + p.px(11)), done, active)
            t = p.text(p.f_h2, text, TEXT if (done or active) else DIM)
            if t.get_width() > card.width - p.px(90):     # shrink long lines on small windows
                t = p.text(p.f_small, text, TEXT if (done or active) else DIM)
            scr.blit(t, (cx + p.px(24), y + p.px(11) - t.get_height() // 2))
            y += p.px(44)
        y += p.px(6)
        tip = "Tip: keep AssistiveTouch ON for a sharp game picture (Settings › Accessibility › Touch)"
        t = p.text(p.f_small, tip, DIM)
        if t.get_width() < card.width - p.px(56):
            scr.blit(t, (card.left + p.px(28), y))
        t = p.text(p.f_small, "Right-click for all options  ·  H for keyboard shortcuts", FAINT)
        scr.blit(t, (card.left + p.px(28), card.bottom - p.px(30)))

    def draw_stats(self, area):
        p, s, a = self.p, self.s, self.audio
        vw, vh = s.video_size
        lines = [
            f"{self.fps:5.1f} fps   {self.mbps:5.2f} Mbps   {s.codec}",
            f"video {vw}x{vh}  orient {s.orientation}  window {area.width}x{area.height}",
            f"decode[{s.decoder_name}] {s.decode_ms:4.1f} ms  draw {s.render_ms:4.1f} ms  window {s.window_ms:4.1f} ms",
            f"late frames skipped {s.frames_skipped}",
            f"audio laptop: {a.device_name}  stream: {self.router.stream.device_name or '-'}  channel: {s.audio_channel}",
            f"sync: {s.sync_text or '-'}",
            f"audio buffer {a.buffered_ms:3.0f}/{a.target_ms} ms  underruns {a.underruns}  skips {a.skips}  "
            f"speed fixes {a.corrections}",
            f"iPhone: {s.info or '-'}",
        ]
        if self.rec.active:
            lines.append(f"recording [{self.rec.encoder_name}] {self.rec.frames} frames, dropped {self.rec.dropped}")
        surfs = [p.text(p.f_mono, line, (120, 255, 140)) for line in lines]
        w = max(x.get_width() for x in surfs) + p.px(20)
        h = sum(x.get_height() + p.px(2) for x in surfs) + p.px(16)
        box = pygame.Surface((w, h), pygame.SRCALPHA)
        box.fill((0, 0, 0, 200))
        y = p.px(8)
        for x in surfs:
            box.blit(x, (p.px(10), y))
            y += x.get_height() + p.px(2)
        self.screen.blit(box, (area.left + p.px(10), area.top + p.px(10)))

    def draw_help(self, area):
        p, scr = self.p, self.screen
        rows = [(p.text(p.f_ui_b, k, ACCENT), p.text(p.f_ui, d, TEXT)) for k, d in HELP_ROWS]
        kw = max(k.get_width() for k, _ in rows)
        dw = max(d.get_width() for _, d in rows)
        rh = p.px(28)
        w = min(area.width - p.px(20), kw + dw + p.px(80))
        h = min(area.height - p.px(20), len(rows) * rh + p.px(90))
        box = pygame.Rect(0, 0, w, h)
        box.center = area.center
        p.panel(scr, box, PANEL, 14)
        t = p.text(p.f_h1, "Keyboard shortcuts", TEXT)
        scr.blit(t, (box.left + p.px(24), box.top + p.px(16)))
        y = box.top + p.px(24) + t.get_height()
        for k, d in rows:
            if y + rh > box.bottom - p.px(30):
                break
            scr.blit(k, (box.left + p.px(24), y))
            scr.blit(d, (box.left + p.px(40) + kw, y))
            y += rh
        t = p.text(p.f_small, "Esc or H to close", FAINT)
        scr.blit(t, (box.right - t.get_width() - p.px(20), box.bottom - p.px(26)))

    def draw_toast(self, area):
        if not self.toasts:
            return
        text, color, t0 = self.toasts[-1]
        age = time.monotonic() - t0
        if age > 2.8:
            self.toasts = []
            self.dirty = True
            return
        p = self.p
        t = p.text(p.f_ui, text, color)
        w, h = t.get_width() + p.px(32), t.get_height() + p.px(16)
        box = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(box, (20, 21, 26, 235), box.get_rect(), border_radius=h // 2)
        pygame.draw.rect(box, (*LINE, 255), box.get_rect(), width=1, border_radius=h // 2)
        box.blit(t, (p.px(16), p.px(8)))
        if age > 2.3:
            box.set_alpha(int(255 * (2.8 - age) / 0.5))
        self.screen.blit(box, (area.centerx - w // 2, area.bottom - h - p.px(24)))
        self.dirty = True                                 # keep animating

    def draw_tooltip(self):
        if not self.hover or self.hover == "slider" or time.monotonic() - self.hover_since < 0.5:
            return
        if self.hover == "sound" and self.meter_rect:
            self.buttons = self.buttons + [("sound", self.meter_rect, "Game sound level - click for the sound panel (A)")]
        for key, r, tip in self.buttons:
            if key == self.hover:
                p = self.p
                t = p.text(p.f_small, tip, TEXT)
                box = pygame.Rect(0, 0, t.get_width() + p.px(16), t.get_height() + p.px(8))
                box.midtop = (r.centerx, r.bottom + p.px(6))
                box.right = min(box.right, self.screen.get_width() - p.px(4))
                box.left = max(box.left, p.px(4))
                p.panel(self.screen, box, PANEL_2, 6)
                self.screen.blit(t, (box.left + p.px(8), box.top + p.px(4)))
                self.tip_shown = True
                return

    def draw(self):
        toolbar, area, statusbar = self.layout()
        self.s.target_size = area.size
        scr = self.screen
        if self.current is not None:
            surf = self.current[0]
            sw, sh = surf.get_size()
            scale = min(area.width / sw, area.height / sh)
            if abs(scale - 1.0) > 0.01:                   # window changed since this frame was made
                surf = pygame.transform.smoothscale(surf, (max(1, int(sw * scale)), max(1, int(sh * scale))))
                sw, sh = surf.get_size()
            if (sw, sh) != area.size:
                scr.fill((0, 0, 0), area)                 # black bars only when needed
            scr.blit(surf, (area.left + (area.width - sw) // 2, area.top + (area.height - sh) // 2))
        else:
            self.draw_waiting(area)
        if toolbar:
            self.draw_toolbar(toolbar)
        else:
            self.buttons, self.slider_rect, self.meter_rect = [], None, None
        if statusbar:
            self.draw_statusbar(statusbar)
        if self.cfg["show_stats"]:
            self.draw_stats(area)
        if self.show_help:
            self.draw_help(area)
        if self.show_sound:
            self.draw_sound_panel(area)
        self.draw_toast(area)
        self.draw_tooltip()

    # ================================================================ main loop
    def tick_stats(self):
        now = time.perf_counter()
        dt = now - self._tick_t
        if dt < 1.0:
            return False
        s = self.s
        self.fps = (s.frames_in - self._tick_frames) / dt
        self.mbps = (s.bytes_total - self._tick_bytes) * 8 / dt / 1e6
        shown = self._shown / dt
        self._tick_frames, self._tick_bytes, self._shown, self._tick_t = s.frames_in, s.bytes_total, 0, now
        self._log_tick += 1
        if self.live and self._log_tick % 5 == 0:     # console log every ~5 s
            a = self.audio
            vw, vh = s.video_size
            print(f"[mirror] {time.strftime('%H:%M:%S')} fps {self.fps:.1f} (shown {shown:.1f}) | "
                  f"{self.mbps:.2f} Mbps | video {vw}x{vh} | {self.mode} | "
                  f"decode[{s.decoder_name}] {s.decode_ms:.1f} ms + draw {s.render_ms:.1f} ms"
                  f" + window {s.window_ms:.1f} ms | late skipped {s.frames_skipped} | "
                  f"audio buf {a.buffered_ms:.0f}/{a.target_ms} ms, underruns {a.underruns}, skips {a.skips}, "
                  f"speed fixes {a.corrections}{' MUTED' if a.muted else ''}"
                  f"{' | REC ' + fmt_time(self.rec.elapsed) if self.rec.active else ''}", flush=True)
        return True

    def run(self):
        s = self.s
        while s.running:
            self.handle_events()
            while self.messages:
                self.toast(*self.messages.popleft())

            # Stream Mode follows the iPhone picture size (portrait <-> landscape)
            if self.mode == "stream" and self.stream_window_size() != self.stream_size:
                self.set_window()

            with s.lock:
                seq, arr = s.frame_seq, s.frame
            if seq != self.last_seq:
                self.last_seq = seq
                self.dirty = True
                if arr is None:
                    self.current = None
                else:
                    h, w = arr.shape[:2]
                    self.current = (pygame.image.frombuffer(arr, (w, h), "BGRA"), arr)
                    self._shown += 1

            if self.tick_stats() and (self.mode == "normal" or self.overlay_visible or self.cfg["show_stats"]):
                self.dirty = True                         # status bar / timers once per second
            sig = (s.state, self.current is None)
            if sig != self.last_sig:
                self.last_sig = sig
                self.dirty = True

            if self.dirty:
                self.dirty = False
                t0 = time.perf_counter()
                self.draw()
                pygame.display.flip()
                s.window_ms = (time.perf_counter() - t0) * 1000
            # Sleep until the video thread has a new frame (or 10 ms for input).
            s.frame_ready.wait(0.010)
            s.frame_ready.clear()

        self.shutdown()

    def shutdown(self):
        if self.rec.active:
            print("[rec] finishing the recording file...", flush=True)
            self.rec.stop(wait_s=15)
        if self.mode == "normal":
            self.windowed_size = self.screen.get_size()
        self.save()
        self.router.close()
        pygame.quit()


def main():
    parser = argparse.ArgumentParser(description="iPhone USB Mirror receiver")
    parser.add_argument("--tcp", help="debug: connect to HOST:PORT instead of usbmux")
    args = parser.parse_args()
    tcp_address = None
    if args.tcp:
        host, port = args.tcp.rsplit(":", 1)
        tcp_address = (host, int(port))
    App(tcp_address).run()


if __name__ == "__main__":
    main()
