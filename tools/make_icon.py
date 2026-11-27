"""make_icon.py - draws the app icon (receiver/icon.ico) with pygame.
Run once: receiver\\.venv\\Scripts\\python.exe tools\\make_icon.py"""

import io
import json
import os
import struct

os.environ["SDL_VIDEODRIVER"] = "dummy"
import pygame

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def draw(size, rounded=True):
    s = pygame.Surface((size, size), pygame.SRCALPHA)
    # gradient rounded square (blue -> purple -> red, like the iPhone app)
    grad = pygame.Surface((size, size), pygame.SRCALPHA)
    a, b = (51, 122, 255), (255, 72, 96)
    for y in range(size):
        t = y / (size - 1)
        pygame.draw.line(grad, [int(a[i] + (b[i] - a[i]) * t) for i in range(3)], (0, y), (size, y))
    mask = pygame.Surface((size, size), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), (0, 0, size, size), border_radius=size // 5 if rounded else 0)
    grad.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    s.blit(grad, (0, 0))
    # phone outline
    w, h = int(size * 0.36), int(size * 0.62)
    x, y = int(size * 0.20), (size - h) // 2
    lw = max(2, size // 22)
    pygame.draw.rect(s, (255, 255, 255), (x, y, w, h), lw, border_radius=size // 12)
    pygame.draw.line(s, (255, 255, 255), (x + w * 0.35, y + lw * 2), (x + w * 0.65, y + lw * 2), max(1, lw // 2 + 1))
    # waves to the right (mirroring / live)
    cx, cy = x + w + size * 0.02, size // 2
    for i, r in enumerate((0.14, 0.24, 0.34)):
        rr = int(size * r)
        rect = pygame.Rect(cx - rr, cy - rr, rr * 2, rr * 2)
        pygame.draw.arc(s, (255, 255, 255), rect, -0.85, 0.85, lw)
    return s


def main():
    pygame.init()
    images = []
    for size in (256, 64, 48, 32, 16):
        buf = io.BytesIO()
        pygame.image.save(draw(size), buf, "icon.png")
        images.append((size, buf.getvalue()))
    # ICO file holding PNG images (supported since Windows Vista)
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    data = b""
    for size, png in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path = os.path.join(ROOT, "receiver", "icon.ico")
    with open(path, "wb") as f:
        f.write(out + data)
    pygame.image.save(draw(256), os.path.join(ROOT, "build", "icon_preview.png"))
    # PC window icon (PNG) and the iPhone app icon (1024, square, no transparency - iOS rounds it)
    pygame.image.save(draw(64), os.path.join(ROOT, "receiver", "icon.png"))
    ios = os.path.join(ROOT, "ios", "App", "Assets.xcassets", "AppIcon.appiconset")
    os.makedirs(ios, exist_ok=True)
    big = pygame.Surface((1024, 1024))
    big.blit(draw(1024, rounded=False), (0, 0))
    pygame.image.save(big, os.path.join(ios, "AppIcon-1024.png"))
    with open(os.path.join(ios, "Contents.json"), "w") as f:
        json.dump({"images": [{"filename": "AppIcon-1024.png", "idiom": "universal",
                               "platform": "ios", "size": "1024x1024"}],
                   "info": {"author": "xcode", "version": 1}}, f, indent=2)
    with open(os.path.join(ROOT, "ios", "App", "Assets.xcassets", "Contents.json"), "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f, indent=2)
    print("icon ->", path)


if __name__ == "__main__":
    main()
