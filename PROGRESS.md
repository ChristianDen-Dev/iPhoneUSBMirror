# iPhone USB Mirror — Progress Log

Read this first when resuming work.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Check computer | ✅ Done (2026-11-27) |
| 1 | Create project | ✅ Done (2026-11-27) |
| 2 | Test USB connection | ⏳ Next |
| 3 | iPhone app (Swift) | — |
| 4 | Cloud build (GitHub Actions) | — |
| 5 | Install with Sideloadly | — |
| 6 | Windows receiver (video) | — |
| 7 | Game audio | — |
| 8 | Polish + OBS | — |
| 9 | Documentation | — |

## Phase 0 — Computer check (2026-11-27)
- Git 2.56.0 ✅
- Python 3.14.8 64-bit ✅ (only version installed; watch for missing package wheels)
- pip 26.2.1 ✅
- GitHub CLI 2.106.0 ✅ (`C:\Program Files\GitHub CLI\gh.exe`) — **not logged in yet** (`gh auth login` needed before Phase 4)
- Apple Devices app 1.1792 ✅ — usbmux runs as `AppleMobileDeviceProcess.exe` on `127.0.0.1:27015` (not a Windows service; that is normal for the Apple Devices app)
- Sideloadly ✅ (`%LOCALAPPDATA%\Sideloadly`)
- OBS Studio 33.0.3 ✅

## Phase 1 — Project created (2026-11-27)
- Folder layout: `ios\`, `receiver\`, `.github\workflows\`, `docs\`, `build\` (ignored)
- Git repo initialized on branch `main`, `.gitignore` added
- Python venv: `receiver\.venv`

## Phase 2 — USB connection ✅ (2026-11-27)
- Installed in venv: pymobiledevice3 11.66.1, av 19.0.2, numpy 2.5.5, pygame-ce 2.5.8, sounddevice 0.5.6 (all have Python 3.14 wheels) → `receiver\requirements.txt`
- Note: pymobiledevice3 11.x is **async** (`await usbmux.list_devices()`, `await MuxDevice.connect(port)` returns a socket).
- `receiver\check_device.py` → **iPhone detected**: iPhone 14 Pro Max, iOS 27.0 (24A437) (UDID kept out of this public repo).
- **Problem fixed:** the iPhone was bound to a `libusb-win32` driver (libwdi/Zadig-generated `oem58.inf`, installed 2026-11-27 08:00), so usbmux only saw it over Network. Removed it with `pnputil /delete-driver oem58.inf /uninstall` (user approved) → Apple driver `appleusb.inf` (oem62.inf) took over → replug → detected over USB. If any tool reinstalls libusb for the iPhone, mirroring will break again.

## Phase 3 — iPhone app ✅ written (2026-11-27, compile check happens in Phase 4)
- `ios\project.yml`: XcodeGen, iOS 16.0, app `com.usbmirror.iphoneusbmirror` + extension `….MirrorBroadcast`, Swift 5 mode, shared scheme.
- `ios\App\MirrorApp.swift`: SwiftUI screen + big `RPSystemBroadcastPickerView` (extension ID looked up at runtime, since Sideloadly may rename IDs).
- `ios\MirrorBroadcast\`:
  - `Protocol.swift`: 14-byte big-endian header, packet types 1–5
  - `MirrorServer.swift`: NWListener on 127.0.0.1:2345 (falls back to any-address), newest client wins, backlog tracking, 1 s heartbeat
  - `VideoEncoder.swift`: VTCompressionSession H.264 Main, real-time, no reordering, 8 Mbps / 12 Mbps cap, keyframe ≤1 s, downscale to long side 1920 (886×1920) via VTPixelTransferSession, SPS/PPS before every keyframe, idle-repeat keyframe
  - `AudioPCM.swift`: audioApp → Float32 (handles big-endian/int/float/mono) → AVAudioConverter → 48 kHz s16le stereo
  - `SampleHandler.swift`: wires it all; drops video if encoder busy or backlog >300 KB; no encoding when no client
- `docs\PROTOCOL.md` written.
- `.github\workflows\build-ios.yml` written (Phase 4).

## Phase 4 — Cloud build ✅ (2026-11-27)
- GitHub account `ChristianDen-Dev`; gh token scopes include `workflow` (needed to push workflow files).
- Repo-local git identity uses the GitHub noreply email (personal email never committed).
- Public repo: https://github.com/ChristianDen-Dev/iPhoneUSBMirror
- First build (run 46028002248) passed in 32 s on Xcode 26.6: no Swift errors/warnings; extension Info.plist verified inside the .ipa.
- Added `UIRequiresFullScreen` to silence the portrait-only warning.
- `.ipa` downloaded to `build\iPhoneUSBMirror.ipa` (git-ignored).
- Re-download a fresh build: `gh run download <run-id> --name iPhoneUSBMirror-ipa --dir build`

## Next
- Phase 5: user installs `build\iPhoneUSBMirror.ipa` with Sideloadly, enables Developer Mode, trusts the profile, checks the broadcast list.

## Problems / notes
- Git has no author identity yet. The repo will be public, so after `gh auth login` set a **repo-local** identity with the GitHub noreply email (`<id>+<login>@users.noreply.github.com`), then make the first commit (files are staged).
