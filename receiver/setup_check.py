r"""
setup_check.py - first-time setup for a new Windows PC (portable package).

Started by "1 - SETUP (first time).bat". It checks everything the mirror
needs, explains in plain words what is missing, and offers to fix it.
Nothing is installed without the user typing Y.

    python setup_check.py           interactive
    python setup_check.py --check   only report (no questions), exit code 0 = all OK
"""

import importlib
import os
import platform
import socket
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))          # ...\app
ROOT = os.path.dirname(HERE)                               # package folder
INSTALLERS = os.path.join(ROOT, "installers")
PYTHON = os.path.join(ROOT, "python", "python.exe")
START_BAT = os.path.join(ROOT, "2 - Start Mirror.bat")
ICON = os.path.join(HERE, "icon.ico")

APPLE_DEVICES_STORE = "ms-windows-store://pdp/?productid=9NP83LWLPZ9K"   # Apple Devices (Microsoft Store)
ITUNES_WEB = "https://www.apple.com/itunes/download/win64"                  # older Windows: iTunes from apple.com
CHECK_ONLY = "--check" in sys.argv

os.system("")   # enable colors in the Windows console
GREEN, YELLOW, RED, BOLD, END = "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[0m"


def ok(msg):
    print(f"  {GREEN}[OK]{END}   {msg}")


def warn(msg):
    print(f"  {YELLOW}[!]{END}    {msg}")


def bad(msg):
    print(f"  {RED}[X]{END}    {msg}")


def ask(question):
    if CHECK_ONLY:
        return False
    try:
        return input(f"         {BOLD}{question} (Y/N): {END}").strip().lower().startswith("y")
    except EOFError:
        return False


def powershell(cmd, wait=True):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    if wait:
        return subprocess.run(args, capture_output=True, text=True, timeout=600).stdout.strip()
    subprocess.Popen(args)
    return ""


def open_url(url):
    os.startfile(url)


# ------------------------------------------------------------------ checks

def check_windows():
    ver = sys.getwindowsversion()
    bits64 = platform.machine().endswith("64")
    name = f"Windows {platform.release()} (build {ver.build}, {platform.machine()})"
    if ver.major < 10:
        bad(f"{name}: Windows 10 or 11 is needed.")
        return False
    if not bits64:
        bad(f"{name}: a 64-bit Windows is needed.")
        return False
    if "ARM" in platform.machine().upper():
        warn(f"{name}: ARM PC - it runs, but may be slower.")
    else:
        ok(name)
    return True


def check_folder():
    p = ROOT.lower()
    if "\\temp\\" in p and ".zip" in p:
        bad("You are running from INSIDE the zip. Close this, right-click the zip -> Extract All, "
            "then run SETUP from the extracted folder.")
        return False
    for sync in ("onedrive", "google drive", "googledrive", "dropbox", "icloud"):
        if sync in p:
            warn(f"The folder is inside a sync folder ({sync}). Better: move it to C:\\iPhoneUSBMirror.")
            return True
    ok(f"Folder: {ROOT}")
    return True


def check_files():
    """The bundled program can start (all libraries load)."""
    sys.path.insert(0, HERE)
    missing = []
    for mod in ("pygame", "av", "numpy", "sounddevice", "pymobiledevice3", "core", "android", "mirror"):
        try:
            importlib.import_module(mod)
        except Exception as e:   # noqa: BLE001 - show any reason
            missing.append(f"{mod} ({e.__class__.__name__}: {e})")
    if missing:
        bad("Some program files cannot load:")
        for m in missing:
            print("           - " + m)
        print("         Most common reason: Windows blocked files downloaded from the internet.")
        print("         Fix: delete this folder, right-click the ZIP -> Properties -> tick Unblock -> OK,")
        print("         Extract All again. If 'Smart App Control' blocks it: Windows Security ->")
        print("         App & browser control -> Smart App Control settings.")
        return False
    ok("Mirror program and its libraries load fine")
    return True


def check_decoder():
    try:
        import av
        from av.codec.hwaccel import HWAccel
        for dev, label in (("cuda", "NVIDIA GPU"), ("d3d11va", "GPU (DirectX 11)")):
            try:
                av.CodecContext.create("h264", "r", hwaccel=HWAccel(device_type=dev, allow_software_fallback=False))
                ok(f"Video decoding on the {label} (fast)")
                return True
            except Exception:   # noqa: BLE001
                continue
        warn("No GPU video decoding - the CPU is used (works, uses more power).")
    except Exception as e:   # noqa: BLE001
        warn(f"Could not test the decoder: {e}")
    return True


def sound_devices():
    try:
        import sounddevice as sd
        return [d["name"] for d in sd.query_devices()]
    except Exception:   # noqa: BLE001
        return []


def check_vbcable():
    if any("CABLE Input" in n for n in sound_devices()):
        ok("VB-Audio Virtual Cable (sound for OBS / TikTok LIVE Studio)")
        return True
    warn("VB-Audio Virtual Cable is not installed. Needed only to STREAM the sound (OBS / TikTok).")
    print("         Watching / recording works without it.")
    if ask("Install it now? (needs administrator, then a PC restart)"):
        src = os.path.join(INSTALLERS, "VBCABLE_Driver_Pack46.zip")
        dest = os.path.join(tempfile.gettempdir(), "VBCABLE_Pack46")
        with zipfile.ZipFile(src) as z:
            z.extractall(dest)
        exe = os.path.join(dest, "VBCABLE_Setup_x64.exe")
        print("         -> Click YES on the Windows question, then 'Install Driver'.")
        powershell(f"Start-Process -FilePath '{exe}' -Verb RunAs -Wait")
        if any("CABLE Input" in n for n in sound_devices()):
            ok("VB-Cable installed. Restart the PC when this setup is finished.")
        else:
            warn("Restart the PC to finish the VB-Cable install (then it shows up).")
    return False


def apple_installed():
    try:
        if powershell("(Get-AppxPackage AppleInc.AppleDevices).Version"):
            return "Apple Devices"
    except Exception:   # noqa: BLE001
        pass
    try:
        import winreg
        for root in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                            name = winreg.QueryValueEx(sub, "DisplayName")[0]
                    except OSError:
                        continue
                    if name in ("iTunes", "Apple Mobile Device Support"):
                        return name
    except Exception:   # noqa: BLE001
        pass
    if powershell("(Get-AppxPackage AppleInc.iTunes).Version"):
        return "iTunes"
    return ""


def usbmux_running():
    try:
        with socket.create_connection(("127.0.0.1", 27015), timeout=0.5):
            return True
    except OSError:
        return False


def check_apple():
    have = apple_installed()
    if have:
        ok(f"{have} (iPhone by USB cable + Sideloadly)" + ("" if usbmux_running() else
           " - its USB service starts when you plug in the iPhone"))
        return True
    warn("Apple Devices is not installed. Needed for: iPhone by USB cable, and Sideloadly.")
    print("         (Not needed for iPhone over Wi-Fi or for Android.)")
    if ask("Open the Microsoft Store page for Apple Devices now? (free; click Get / Install)"):
        try:
            open_url(APPLE_DEVICES_STORE)
        except OSError:
            open_url(ITUNES_WEB)
        print("         If the Store does not open (old Windows), install iTunes from apple.com:")
        print("         " + ITUNES_WEB)
    return False


def sideloadly_installed():
    for p in (os.path.expandvars(r"%LOCALAPPDATA%\Sideloadly\sideloadly.exe"),
              r"C:\Program Files\Sideloadly\sideloadly.exe",
              r"C:\Program Files (x86)\Sideloadly\sideloadly.exe"):
        if os.path.isfile(p):
            return True
    return os.path.isdir(os.path.expandvars(r"%LOCALAPPDATA%\Sideloadly"))


def check_sideloadly():
    if sideloadly_installed():
        ok("Sideloadly (puts the USB Mirror app on the iPhone)")
        return True
    warn("Sideloadly is not installed. Needed only to INSTALL / RENEW the app on an iPhone.")
    if ask("Install Sideloadly now?"):
        exe = os.path.join(INSTALLERS, "SideloadlySetup64.exe")
        print("         -> Click YES on the Windows question and follow the installer.")
        powershell(f"Start-Process -FilePath '{exe}' -Wait")
        if sideloadly_installed():
            ok("Sideloadly installed")
    return False


def make_shortcut():
    desk = powershell("[Environment]::GetFolderPath('Desktop')")
    if not desk:
        return
    lnk = os.path.join(desk, "iPhone USB Mirror.lnk")
    if os.path.isfile(lnk):
        ok("Desktop shortcut 'iPhone USB Mirror' is there")
        return
    if not ask("Put an 'iPhone USB Mirror' shortcut on the Desktop?"):
        return
    icon = ICON if os.path.isfile(ICON) else PYTHON
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          "$s.TargetPath='{bat}';$s.WorkingDirectory='{root}';$s.IconLocation='{icon}';"
          "$s.Description='Mirror your phone to this PC';$s.Save()").format(
        lnk=lnk.replace("'", "''"), bat=START_BAT.replace("'", "''"),
        root=ROOT.replace("'", "''"), icon=icon.replace("'", "''"))
    powershell(ps)
    if os.path.isfile(lnk):
        ok("Desktop shortcut made")


def main():
    print()
    print(f"{BOLD}  iPhone USB Mirror - first-time setup{END}")
    print("  ------------------------------------")
    print("  Checking this PC. Nothing is installed unless you type Y.\n")

    results = {
        "windows": check_windows(),
        "folder": check_folder(),
        "files": check_files(),
    }
    if results["files"]:
        check_decoder()
    print()
    print(f"{BOLD}  Optional parts (only for some uses){END}")
    results["apple"] = check_apple()
    results["sideloadly"] = check_sideloadly()
    results["vbcable"] = check_vbcable()
    print()
    if results["files"] and not CHECK_ONLY:
        make_shortcut()

    print()
    print(f"{BOLD}  Summary{END}")
    must = results["windows"] and results["folder"] and results["files"]
    if must:
        ok("This PC can run the mirror.  Start it with  '2 - Start Mirror.bat'  (or the Desktop shortcut).")
    else:
        bad("Fix the red [X] item(s) above, then run SETUP again.")
    print("         Android: nothing else to install (turn on USB debugging on the phone).")
    print("         iPhone : install the app with Sideloadly - see 'START HERE (guide).html'.")
    print()
    return 0 if must else 1


if __name__ == "__main__":
    code = main()
    if not CHECK_ONLY:
        try:
            input("  Press Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
