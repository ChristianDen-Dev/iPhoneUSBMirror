r"""
make_portable.py - builds the "copy to any Windows PC" package.

    receiver\.venv\Scripts\python.exe tools\make_portable.py

Result:  dist\iPhoneUSBMirror_Portable\        (folder)
         dist\iPhoneUSBMirror_Portable.zip     (upload this to Google Drive)

The package needs NO Python install on the other PC: it carries the official
"embeddable" Python from python.org plus all libraries, the Android tools
(adb + scrcpy-server from the official scrcpy release) and the installers
you still need there (VB-Audio Cable, Sideloadly, the iPhone .ipa).

Downloads are cached in build\dl so a rebuild is fast.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECEIVER = os.path.join(ROOT, "receiver")
DL = os.path.join(ROOT, "build", "dl")
DIST = os.path.join(ROOT, "dist")
NAME = "iPhoneUSBMirror_Portable"
OUT = os.path.join(DIST, NAME)

PY_VERSION = "3.14.8"                       # same as the development venv
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
SCRCPY_VERSION = "5.0.1"                    # must match android.SERVER_VERSION
SCRCPY_URL = (f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/"
              f"scrcpy-win64-v{SCRCPY_VERSION}.zip")
SCRCPY_SHA256 = "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5"
SIDELOADLY_URL = "https://sideloadly.io/SideloadlySetup64.exe"

APP_FILES = ["mirror.py", "core.py", "android.py", "audio_player.py", "recorder.py", "ui.py", "winutil.py",
             "check_device.py", "setup_check.py", "icon.ico", "icon.png", "requirements.txt"]
ANDROID_FILES = ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "scrcpy-server", "LICENSE.txt"]


def log(msg):
    print(f"[portable] {msg}", flush=True)


def download(url, name, sha256=None):
    os.makedirs(DL, exist_ok=True)
    path = os.path.join(DL, name)
    if not os.path.isfile(path):
        log(f"downloading {url}")
        tmp = path + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (portable-builder)"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(tmp, path)
    if sha256:
        h = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if h != sha256:
            raise SystemExit(f"checksum mismatch for {name}: {h}")
    return path


def android_tools(dest):
    """adb + scrcpy-server from the official scrcpy release."""
    z = download(SCRCPY_URL, f"scrcpy-win64-v{SCRCPY_VERSION}.zip", SCRCPY_SHA256)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        for f in ANDROID_FILES:
            info = zf.getinfo(f"scrcpy-win64-v{SCRCPY_VERSION}/{f}")
            target = os.path.join(dest, f)
            if os.path.isfile(target) and os.path.getsize(target) == info.file_size:
                continue                    # already there (adb.exe may be running = locked)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def portable_python(dest):
    z = download(PY_URL, f"python-{PY_VERSION}-embed-amd64.zip")
    with zipfile.ZipFile(z) as zf:
        zf.extractall(dest)
    # ._pth decides sys.path for the embedded Python: add our libraries + the app
    pth = next(f for f in os.listdir(dest) if f.endswith("._pth"))
    zipname = next(f for f in os.listdir(dest) if f.startswith("python3") and f.endswith(".zip"))
    with open(os.path.join(dest, pth), "w", encoding="ascii", newline="\r\n") as f:
        f.write(f"{zipname}\n.\nLib\\site-packages\n..\\app\nimport site\n")
    site = os.path.join(dest, "Lib", "site-packages")
    log("installing libraries (pip)")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                           "--no-warn-script-location", "--target", site,
                           "-r", os.path.join(RECEIVER, "requirements.txt")])


def main():
    if os.path.isdir(OUT):
        log(f"removing old {OUT}")
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # also keep the development copy of the Android tools up to date
    android_tools(os.path.join(RECEIVER, "vendor", "android"))

    log("app files")
    app = os.path.join(OUT, "app")
    os.makedirs(app)
    for f in APP_FILES:
        shutil.copy2(os.path.join(RECEIVER, f), app)
    shutil.copytree(os.path.join(RECEIVER, "tools"), os.path.join(app, "tools"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    android_tools(os.path.join(app, "vendor", "android"))

    log("portable Python")
    portable_python(os.path.join(OUT, "python"))

    log("installers")
    inst = os.path.join(OUT, "installers")
    os.makedirs(inst)
    shutil.copy2(os.path.join(ROOT, "build", "vbcable", "VBCABLE_Driver_Pack46.zip"), inst)
    shutil.copy2(os.path.join(ROOT, "build", "iPhoneUSBMirror.ipa"), inst)
    shutil.copy2(download(SIDELOADLY_URL, "SideloadlySetup64.exe"), inst)

    log("start files + guide")
    for name in ("1 - SETUP (first time).bat", "2 - Start Mirror.bat", "START HERE.txt", "START HERE (guide).html"):
        shutil.copy2(os.path.join(ROOT, "tools", "portable", name), OUT)

    log("self-test: import everything with the portable Python")
    subprocess.check_call([os.path.join(OUT, "python", "python.exe"), "-c",
                           "import core, android, audio_player, recorder, ui, winutil, mirror, pygame, av, "
                           "sounddevice, numpy, pymobiledevice3; print('[portable] self-test OK', android.available())"],
                          cwd=app)
    log("self-test: setup checker (report only)")
    subprocess.call([os.path.join(OUT, "python", "python.exe"), os.path.join(app, "setup_check.py"), "--check"], cwd=OUT)

    zpath = os.path.join(DIST, NAME + ".zip")
    log(f"zipping -> {zpath}")
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for folder, _, files in os.walk(OUT):
            if "__pycache__" in folder:
                continue
            for f in files:
                full = os.path.join(folder, f)
                zf.write(full, os.path.join(NAME, os.path.relpath(full, OUT)))
    size = os.path.getsize(zpath) / 1e6
    log(f"done: {zpath}  ({size:.0f} MB)")


if __name__ == "__main__":
    main()
