"""
ui.py - look of the receiver window: colors, fonts, icons and small drawing
helpers. Everything is drawn with pygame; icons come from the Windows font
"Segoe MDL2 Assets". Sizes follow the Windows display scale (100-200 %).
"""

import os

import pygame

# ---- dark theme -------------------------------------------------------------
BG = (11, 12, 15)
PANEL = (24, 25, 30)
PANEL_2 = (34, 36, 43)
LINE = (46, 48, 57)
TEXT = (234, 236, 242)
DIM = (150, 154, 166)
FAINT = (98, 102, 115)
HOVER = (52, 55, 65)
PRESSED = (64, 68, 80)
ON = (35, 76, 150)
ACCENT = (72, 148, 255)
RED = (240, 72, 72)
GREEN = (52, 200, 98)
AMBER = (255, 182, 46)
ORANGE = (255, 132, 44)

HEAT_COLORS = {"nominal": GREEN, "fair": AMBER, "serious": ORANGE, "critical": RED}

ICON_FONT = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segmdl2.ttf")

# Segoe MDL2 Assets glyphs
ICONS = {
    "stream": "\ue7f4", "full": "\ue740", "unfull": "\ue73f", "shot": "\ue722",
    "rec": "\ue7c8", "stop": "\ue71a", "flip": "\ue7ad", "pin": "\ue718", "unpin": "\ue77a",
    "vol": "\ue767", "mute": "\ue74f", "stats": "\ue9d9", "folder": "\ued25", "help": "\ue897",
    "menu": "\ue713", "info": "\ue946",
}


class Painter:
    def __init__(self, scale=1.0):
        self.scale = scale
        px = self.px
        self.f_ui = pygame.font.SysFont("segoeui", px(14))
        self.f_ui_b = pygame.font.SysFont("segoeui", px(14), bold=True)
        self.f_small = pygame.font.SysFont("segoeui", px(12))
        self.f_hd = pygame.font.SysFont("segoeui", px(12), bold=True)
        self.f_h1 = pygame.font.SysFont("segoeui", px(24), bold=True)
        self.f_h2 = pygame.font.SysFont("segoeui", px(16))
        self.f_mono = pygame.font.SysFont("consolas", px(13))
        try:
            self.f_icon = pygame.font.Font(ICON_FONT, px(16))
        except Exception:
            self.f_icon = None
        self._cache = {}

    def px(self, v):
        return max(1, int(round(v * self.scale)))

    def text(self, font, s, color=TEXT):
        """Rendered text surface (cached: drawing the same label 60x/s is free)."""
        key = (id(font), s, color)
        surf = self._cache.get(key)
        if surf is None:
            if len(self._cache) > 600:
                self._cache.clear()
            surf = font.render(s, True, color)
            self._cache[key] = surf
        return surf

    def icon(self, name, color=TEXT):
        if self.f_icon is None:
            return self.text(self.f_small, name[:3].upper(), color)
        return self.text(self.f_icon, ICONS.get(name, "?"), color)

    def blit_center(self, screen, surf, rect):
        screen.blit(surf, (rect.centerx - surf.get_width() // 2, rect.centery - surf.get_height() // 2))

    def pill(self, screen, x, cy, label, color, font=None):
        """Rounded label with a colored dot; returns its width."""
        font = font or self.f_small
        t = self.text(font, label, TEXT)
        pad, dot = self.px(10), self.px(4)
        w = pad + dot * 2 + self.px(7) + t.get_width() + pad
        h = self.px(24)
        r = pygame.Rect(x, cy - h // 2, w, h)
        pygame.draw.rect(screen, PANEL_2, r, border_radius=h // 2)
        pygame.draw.circle(screen, color, (x + pad + dot, cy), dot)
        screen.blit(t, (x + pad + dot * 2 + self.px(7), cy - t.get_height() // 2))
        return w

    def panel(self, screen, rect, color=PANEL, radius=10, border=LINE):
        pygame.draw.rect(screen, color, rect, border_radius=self.px(radius))
        if border:
            pygame.draw.rect(screen, border, rect, width=1, border_radius=self.px(radius))

    def check_circle(self, screen, center, done, active):
        """Step marker: green check (done), blue ring (current), grey ring (later)."""
        r = self.px(11)
        if done:
            pygame.draw.circle(screen, GREEN, center, r)
            x, y = center
            pts = [(x - self.px(5), y), (x - self.px(1), y + self.px(4)), (x + self.px(6), y - self.px(4))]
            pygame.draw.lines(screen, BG, False, pts, self.px(2.5))
        else:
            pygame.draw.circle(screen, ACCENT if active else FAINT, center, r, self.px(2))


def make_window_icon():
    """32x32 app icon: a blue rounded square with a white phone outline."""
    s = pygame.Surface((32, 32), pygame.SRCALPHA)
    pygame.draw.rect(s, ACCENT, (0, 0, 32, 32), border_radius=7)
    pygame.draw.rect(s, (255, 255, 255), (10, 4, 12, 24), width=2, border_radius=3)
    pygame.draw.line(s, (255, 255, 255), (14, 7), (17, 7), 2)
    return s
