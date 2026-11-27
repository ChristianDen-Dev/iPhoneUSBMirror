# iPhone USB Mirror — Progress Log

Read this first when resuming work.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Check computer | ✅ Done (2026-11-27) |
| 1 | Create project | ✅ Done (2026-11-27) |
| 2 | Test USB connection | ✅ Done |
| 3 | iPhone app (Swift) | ✅ Done |
| 4 | Cloud build (GitHub Actions) | ✅ Done |
| 5 | Install with Sideloadly | ✅ Done |
| 6 | Windows receiver (video) | ✅ Works (sharp with AssistiveTouch trick) |
| 7 | Game audio | ✅ Works |
| 8 | Polish + OBS | 🔧 In progress (new window, launcher done; OBS guide + 30-min test left) |
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

## Phase 5 — Install ✅ (2026-11-27)
- Installed with Sideloadly (free Apple ID). Sideloadly renamed the bundle ID to `com.usbmirror.iphoneusbmirror.23KRTS7HNT` (handled: the app finds its extension at runtime).
- Needed: Developer Mode ON + trust the developer profile (VPN & Device Management).

## Phase 6 — Video receiver ✅ (2026-11-28)
- `receiver\mirror.py` (pygame-ce window "iPhone USB Mirror", PyAV decode, aspect-fit, F11/Esc/F1/R, auto-reconnect, console stats every 5 s).
- `receiver\tools\fake_iphone.py`: protocol-exact fake sender (libx264 + test tone) for testing without the phone (`mirror.py --tcp 127.0.0.1:2345`).
- **Smart App Control** blocked `av\format.pyd` of PyAV 19.0.2 → pinned **PyAV 19.0.1** (yanked only for an audio-decoding bug we don't use). SAC left ON (turning it off is irreversible).
- Bug found by fake test and fixed: non-contiguous numpy array in portrait.
- **Real iPhone test:** works over USB; Mobile Legends at a steady **60 fps, ~9.5 Mbps, decode 7–13 ms**. Landscape was upside-down → 6/8 rotation swapped (now verified correct).
- **Open issue:** in Mobile Legends the stream is only **408×886** (portrait home screen is 886×1918) → looks blurry when enlarged. Cause unknown: the iPhone hands the extension a smaller buffer. Added an `INFO` packet (type 6) reporting source buffer size/format, encode size, memory, thermal state, Low Power Mode → needs re-install of the new build (run 46034288497) to diagnose.
- The extension's NSLog lines are not visible via `pymobiledevice3 syslog live` (only ReplayKit/system lines are), hence the INFO packet.

## Phase 7 — Game audio ✅ (2026-11-28)
- `receiver\audio_player.py`: sounddevice RawOutputStream 48 kHz s16 stereo, 5 ms blocks, 80 ms jitter buffer, re-buffer on underrun, skip-ahead above 200 ms (no delay build-up). `M` = mute.
- Fake-tone test and real iPhone: plays on "Speakers (Realtek(R) Audio)", 0 underruns, 0 skips.
- Still to confirm by the user: game sound heard; OBS "Desktop Audio" picks it up (Phase 8).

## Sharpness investigation ✅ SOLVED (2026-11-28)
- INFO packet showed: ReplayKit hands the extension **886×1918** for normal screens, but only **408×886** while Mobile Legends is the only thing on screen (exactly 1/3 of the game's ~1224×2658 render size). Not our code, not bitrate, not Game Mode (tested off), not ML HD mode (already on), not frame rate.
- Cause: iOS shows a lone full-screen game "direct to display"; screen capture then gets a reduced copy. Whenever any system overlay is on screen (Control Center, banner) the compositor is used and capture is full size.
- **Fix:** turn on **AssistiveTouch** (Settings → Accessibility → Touch → AssistiveTouch, Idle Opacity 15%). Its floating button keeps the compositor active → ML captured at full **1918×886, 60 fps** (verified 2026-11-28 00:51).
- Side effect: the AssistiveTouch button IS visible in the capture → park it at a screen edge/corner (and/or crop in OBS).
- Receiver also gained Lanczos + unsharp-mask upscaling (`S` key) for any small frames.
- Thermal state reads SERIOUS during long sessions (charging + game + encode); watch it in the 30-min test.

## Max quality upgrade (2026-11-28)
- iPhone: H.264 **High**, 20 Mbps (peak 30), keyframe every 2 s → `stream: H.264 High, 886x1918`.
- Receiver split into threads: network / GPU video decode (cuda, d3d11va, CPU fallback) / window. Event-driven redraw. Decode ~3 ms + draw ~2 ms.
- Real result: **1918×886 at 45–55 fps** in Mobile Legends (lobby ~30 fps = game's own rate), ~22 Mbps. iPhone heat reads SERIOUS → may limit fps.

## Phase 8a — New app window ✅ (2026-11-28)
Code is now split: `core.py` (USB/decode engine), `mirror.py` (window), `ui.py` (look), `recorder.py`, `winutil.py`, `audio_player.py`.
- **Normal mode:** real Windows window (dark title bar, resizable) + toolbar + status bar (codec, size, fps, Mbps, GPU, iPhone heat, audio, volume) + step-by-step connect screen.
- **Stream Mode (F2):** keeps a real title bar (option to hide it), client area = exact 1:1 picture, cursor auto-hides, right-click = native Windows menu (not visible in OBS capture). Window placed by its visible edges (no off-screen pixels).
- **Fullscreen (F11 / double-click):** controls appear on mouse move.
- **Screenshot (F12)** → `Pictures\iPhone USB Mirror\*.png` (full resolution, upright).
- **Recording (F9)** → `Videos\iPhone USB Mirror\*.mp4`, NVENC (RTX 5060) H.264 20 Mbps + AAC 192k, upright, raw game sound; fallbacks AMF/QSV/MF/x264. Tested: 157 frames + 6.6 s audio OK.
- Volume slider 0–150 %, mute, always on top, flip, sharpen, stats (F1), help (H), open folder (O).
- `receiver\start_mirror.bat`: double-click launcher.

## Audio delay fix (2026-11-28)
- User: laptop sound lags the phone. Measured laptop part: old MME output **90 ms** + buffer ~116 ms ≈ **200 ms**.
- Now: **WASAPI** output (22 ms) + **adaptive** buffer (starts 40 ms, +10 ms per dropout up to 120, shrinks after 30 s clean) + smooth ±1.25 % speed drift correction instead of jumps. Real iPhone: settles ~60 ms → laptop part ≈ **80 ms**.
- The rest is ReplayKit on the iPhone (it delivers app audio in chunks after it plays); cannot be zero. Phone speaker + laptop together will always echo slightly → turn the iPhone volume down while streaming.

## Audio latency measurements (2026-11-28, new iPhone build run 46043497534)
- Added sound-only USB channel (iPhone port 2346) + SyncMeter (`[sync]` log lines) + iPhone-side audio delivery stats in INFO.
- ReplayKit hands the extension **23.2 ms pieces, 43/s, with gaps up to ~45 ms** (measured INSIDE the iPhone) → this unevenness is iOS's, not USB/our code.
- A/B test (same session, alternating): separate channel vs main channel → identical (jitter 28–45 ms, sound≈video arrival). Channel kept (harmless, protects against big keyframes). `MIRROR_AUDIO_CHANNEL=0` disables it for tests.
- Laptop part now: WASAPI shared 22 ms (driver floor; exclusive mode would hide sound from OBS/TikTok Desktop Audio → not used) + adaptive buffer ~30–40 ms ≈ **55–60 ms** (was ~200 ms). Remaining delay vs phone speaker is ReplayKit's own.

## Stream sound routing + iPhone mic (2026-11-28)
- User approved installing **VB-Audio Virtual Cable** (Pack46, signed by Vincent Burel). Installer made "CABLE Input" the Windows default speaker → restored Realtek with `receiver\tools\set_default_speaker.ps1`; the app now auto-restores it at start.
- Own virtual sound device inside our app is not possible without a Microsoft-signed kernel driver (paid EV cert) → VB-Cable is the practical way.
- `AudioRouter`: game sound → **CABLE Input** (stream, always 100 %) + **laptop speakers** (own volume, M = off). Laptop mute never mutes the stream.
- TikTok LIVE Studio / OBS: Desktop audio = CABLE Input. Mic = their own mic source, OR:
- **iPhone mic option** (iPhone build run 46046741868): mic button shown in the Start Broadcast sheet; packet type 7; mixed into the stream output only (never laptop speakers). N = mute, loudness 0-300 % in the menu. Verified by recording CABLE Output: game + mic tones both present with laptop muted.
- UI: toolbar level meter + mic button (when the iPhone mic is on), Sound panel (A), status bar shows stream/mic/laptop state.
- Recordings (F9) contain game sound only (not the mic) for now.

## Next
- Phase 8b: OBS / TikTok LIVE Studio capture guide, 30-min stability test.
- Phase 9: docs.

## Problems / notes
- Git has no author identity yet. The repo will be public, so after `gh auth login` set a **repo-local** identity with the GitHub noreply email (`<id>+<login>@users.noreply.github.com`), then make the first commit (files are staged).
