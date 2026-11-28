# Den-Dev Live

Mirror your phone to a Windows PC for OBS / TikTok LIVE Studio, or go live
straight from your iPhone. Free, no watermark, no time limit.

**Version 1.0**

## What it does

| | |
|---|---|
| **iPhone → PC** | USB cable (lowest delay) or Wi-Fi. Picture + game sound. |
| **Android → PC** | USB or Wi-Fi, no app needed on the phone (scrcpy server). Mouse = finger. |
| **PC window** | Stream Mode (clean picture, exact size), record, screenshot, sound to OBS through VB-Cable. |
| **Go Live (iPhone only)** | The phone streams to Facebook / YouTube / TikTok / any RTMP server by itself. |
| **Studio** | Landscape / portrait, fit / blur / fill, your logo, banner text, LIVE badge, "Be right back" screen. |
| **Comments** | See-through floating comments over the game (TikTok style), drag the arrow to any side. |
| **Den-Dev Live mic** | The app records your voice in normal mode, so iOS does not turn the game sound down. |

## Download and install

1. Download `DenDevLive_Portable.zip` (for example from Google Drive).
2. Right-click the zip → Properties → tick **Unblock** → OK → Extract All → `C:\DenDevLive`.
3. Run **`1 - SETUP (first time).bat`**. It checks the PC and offers Apple Devices, Sideloadly and VB-Cable.
4. Start with the Desktop shortcut **Den-Dev Live**.
5. iPhone: install `installers\DenDevLive.ipa` with Sideloadly (your own Apple ID, typed only into Sideloadly),
   then trust it in Settings → General → VPN & Device Management, and turn on Developer Mode.

The full illustrated guide is `START HERE (guide).html` in the package.

## Project layout

| Folder | What |
|---|---|
| `ios/` | Swift app + ReplayKit Broadcast Upload Extension (XcodeGen `project.yml`, HaishinKit 2.2.5 for Go Live) |
| `receiver/` | Python Windows receiver (pygame + PyAV, usbmux, Wi-Fi, Android) |
| `tools/` | `make_portable.py` (builds the zip), `make_icon.py`, package files |
| `docs/PROTOCOL.md` | Phone → PC packet format |
| `.github/workflows/` | Cloud build of the unsigned .ipa (macOS runner) |

## Build

- iPhone app: push to `main` → GitHub Actions builds `iPhoneUSBMirror-ipa` (unsigned, sign it with Sideloadly).
- PC package: `receiver\.venv\Scripts\python.exe tools\make_portable.py` → `dist\DenDevLive_Portable.zip`.

## Privacy

Nothing is sent anywhere except to your PC (mirror) or to the live server you choose (Go Live).
The stream key stays in the iPhone Keychain. No Apple ID, password or key is stored in this repository.

## License

GPL-3.0, see [LICENSE](LICENSE) (the PC side uses pymobiledevice3, which is GPL-3.0). Uses HaishinKit (BSD-3), scrcpy server (Apache-2.0), PyAV, pygame-ce.
