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
| 10 | iPhone over Wi-Fi | 🔧 Code + build done (run 46065362382); waiting for real-phone test |
| 11 | Android (USB + Wi-Fi, touch control) | 🔧 Code done; waiting for an Android phone to test |
| 12 | Portable package (Google Drive) | ✅ Built `dist\iPhoneUSBMirror_Portable.zip` (265 MB), self-test OK |

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

## Auto mic findings (2026-11-28)
- Broadcast **Microphone ON** puts iOS in voice-chat mode: phone speaker gets quiet and it fights with the Mobile Legends voice mic. Recommended: Microphone **OFF** in the broadcast sheet, stream voice from the laptop mic (`mic_source` Auto falls back to the laptop mic within 0.8 s when the iPhone mic is silent / absent).

## Phase 10 — iPhone over Wi-Fi (2026-11-28)
- iOS (run 46065362382): `MirrorServer` now listens on all addresses (USB via 127.0.0.1 still works); Wi-Fi clients get **12 Mbps** (USB 20); INFO shows `link wifi|usb`; app asks for **Local Network** permission (Bonjour `_usbmirror._tcp`) and shows the iPhone's Wi-Fi / Hotspot IP.
- Receiver: order = iPhone USB → Android → iPhone Wi-Fi. `WiFiFinder` tries remembered IPs, the Hotspot address 172.20.10.1, and a ~1 s non-blocking scan of the laptop's /24 for port 2345 (every 15 s while idle; **W** = now). While on Wi-Fi it checks USB every 2 s and **switches to USB** when the cable is plugged in. Sound-only channel 2346 follows the same link.
- Tested with `fake_iphone.py` through the Wi-Fi code path (`--tcp`): connects, GPU decode, sound OK. Real iPhone test pending (needs the new .ipa via Sideloadly).
- **Connection button** (first toolbar button, key **L**, menu radio items): Auto (USB first, then Wi-Fi; auto-switch to USB when plugged) / USB only / Wi-Fi only. Saved as `link_mode`. Changing it drops a connection the new mode does not allow. Wi-Fi only + Android on USB = moves the phone to Wi-Fi automatically (adb tcpip).
- Honest limit: Wi-Fi adds ~5-30 ms and can stutter on busy 2.4 GHz; USB stays the lowest delay. Best wireless: 5 GHz router or the laptop on the iPhone Hotspot.

## Phase 11 — Android (2026-11-28)
- No phone app needed: `android.py` uses **adb + scrcpy-server v5.0.1** (Apache 2.0, from the official release, SHA-256 checked) in `receiver\vendor\android` (git-ignored; `tools\make_portable.py` downloads it). We do not run scrcpy.exe — our own window/decoder/sound router/recorder are used.
- Protocol v5 (read from the v5.0.1 source, not memory): 3 forward sockets video/audio/control; video: dummy byte, 64-byte name, codec id, 12-byte headers (MSB=1 session packet w/h; else bit62 config, bit61 key, 61-bit PTS); audio `raw` = 48 kHz s16le stereo.
- USB 16 Mbps / Wi-Fi 8 Mbps, max 1920, 60 fps. Sound needs Android 11+. Default `audio_source=output` (phone speaker silent); menu option "keep sound on the phone" = `playback` + `audio_dup` (Android 13+, some games may block it).
- Control: mouse = finger (touch messages, 32 bytes), Backspace/Home/End = Back/Home/Recents, toolbar buttons when Android is connected.
- Android Wi-Fi: menu "Android: switch to Wi-Fi" (`adb tcpip 5555` + `adb connect IP`), IP remembered and reconnected automatically.
- adb's mDNS makes Windows Firewall ask → we start adb with `ADB_MDNS=0`; adb then listens on 127.0.0.1:5037 only (checked).

## Phase 12 — Portable package (2026-11-28)
- `tools\make_portable.py` → `dist\iPhoneUSBMirror_Portable\` + `.zip` (265 MB): official embeddable Python 3.14.8 + libraries (pip --target), app, adb/scrcpy-server, installers (VB-Cable Pack46, Sideloadly, our .ipa), `Start Mirror.bat`, `START HERE.txt`.
- Self-test imports everything with the portable Python; window opened and found VB-Cable + laptop mic when run from `dist`.
- Other PC still needs: VB-Cable install (admin + restart) for streaming; Apple Devices app only for iPhone **USB** (Wi-Fi and Android need nothing).

## Phase 13 — New app design + Go Live (phone streams by itself) (2026-11-28)
- Researched PRISM Live Studio / TikTok LIVE: they offer screen broadcast to many platforms, a chat view that can be shown/hidden, a privacy screen during screencast, per-source volume. We copied that idea set.
- **Home screen**: 3 big buttons — **USB** (lowest delay), **Wi-Fi** (wireless), **Go Live** (no PC). Dark glass design with color glows per mode. The screen you open last decides what the next Start Broadcast does.
- **Go Live**: platform cards (Facebook / YouTube / TikTok / Custom RTMP), server prefilled, stream key in the **Keychain** (eye / paste buttons, "Where do I get my key?" link), quality presets (480p, 720p30, 720p60, 1080p30), landscape/portrait, game + mic volume, checklist, big GO LIVE button.
- Extension streams with **HaishinKit 2.2.5** (BSD-3, SPM): frames rotated upright + fitted to 16:9 / 9:16 on the GPU, game sound + mic mixed, adaptive bitrate, auto-reconnect (10 tries), still screen keeps sending frames.
- Settings app → extension without App Group: app serves JSON on 127.0.0.1:2399; extension asks at start and **every second** (live controls). Status back on 127.0.0.1:2347.
- **Live dashboard**: LIVE timer, upload kbps, quality, fps, size, reconnects, stream health.
- **Comments panel** (like TikTok LIVE): arrow tab on the right edge → panel slides in; arrow again (or swipe right) → hides. Built-in browser shows the platform's live chat; you sign in there yourself. Paste your live video link for the cleanest view (YouTube links become the live-chat page). Screen stays awake while it is open.
- **Privacy screen**: "Screen hidden" → viewers see a "Be right back" card (sound continues). **Mic mute** button. Both in the panel and on the Go Live screen.
- CI build 46072723838 OK → `build\iPhoneUSBMirror.ipa`; portable zip rebuilt (266 MB).
- Limit: iOS does not let any app draw over other apps, so comments show when you switch back to USB Mirror (swipe the home bar), not on top of the game.
- Real-phone test pending: install the new .ipa with Sideloadly, check USB/Wi-Fi still work, then Go Live to Facebook with privacy "Only me".

## Phase 14 - Install anywhere + Studio + TikTok-style comments (2026-11-28)
- **Portable package**: `1 - SETUP (first time).bat` removes the Google Drive "downloaded from internet" mark (Unblock-File) and runs `setup_check.py`: Windows 10/11 64-bit, folder (not inside zip / sync folder), all libraries load, GPU decoder, Apple Devices (Store page), Sideloadly, VB-Cable (admin install), Desktop shortcut with the app icon. `2 - Start Mirror.bat`. `START HERE (guide).html` = illustrated guide (download, PC setup, iPhone Sideloadly + Trust + Developer Mode, Android USB debugging, OBS, Go Live, problems).
- Tested like a new PC: zip marked as internet download (ZoneId=3), Explorer extract (12,746 files marked) -> SETUP -> 0 marked, all OK with no Python on PATH -> mirror starts and finds the audio devices.
- **Logo**: one icon (phone + waves, blue->red) for the iPhone app (AppIcon 1024), the PC window and the Desktop shortcut (`tools\make_icon.py`).
- **Studio** (Go Live -> Studio): live preview of what viewers see; Landscape/Portrait; Fit + blur / Fit / Fill; logo from Photos (corner, size); banner text (4 colors); LIVE badge with timer; custom "Be right back" text. The extension draws it on the GPU (Core Image compositor) and re-reads the settings every second, so changes show on a running live.
- **Comments like TikTok**: draggable arrow handle (snaps to left/right edge), tap -> see-through floating comments (no box, text shadow, old ones fade), drag the feed anywhere, see-through 40-100%, tap anywhere else -> hide. Comments are read from the chat page (YouTube live chat, TikTok LIVE web, Facebook comments). "Full chat" sheet to sign in / reply. "Float over game" = Picture-in-Picture window over the game (iOS only allows PiP over other apps; swipe it to the edge -> arrow tab). Beta: needs a real-phone test (background reading of comments).
- CI 46075059871 OK -> build\iPhoneUSBMirror.ipa; portable zip rebuilt (266 MB).
- Latency (Wi-Fi mirror log): decode 2-6 ms, sound within +-10 ms of video; the delay spikes (up to ~240 ms) come from the Wi-Fi link (signal 53 %, Realtek "Leisure Power Save" = Auto). USB has none.

## Phase 15 - Go Live fixes + rename to "Den-Dev Live" (2026-11-28)
- User report: Facebook "trouble playing this video", picture upside down, audio crackling, live stopped; alert "RTMPConnection.Error error 3".
- Read the HaishinKit 2.2.5 source: RTMP audio/video timestamps each start at their own first packet; multi-track mixer only outputs when the main track has data; NWConnection `.waiting` / viability=false closes the socket at once (= error 3 socketErrorOccurred).
- Fixes: (1) AudioClock: our own 48 kHz mixer on a steady host-clock timer (game + mic converted, 100 ms buffer, 300 ms cap, limiter) -> continuous audio from the first second, single-track HaishinKit mixer; (2) video timestamps forced increasing and sent in order; (3) rotation fixed (ReplayKit 6 = 90 CCW -> CIImage .left), same table as the laptop receiver; (4) connect up to 6 tries with a fresh session each time, reconnect with a fresh session, ignore stale disconnect callbacks, stop only after >10 drops in 2 minutes; reasons logged.
- No crash / jetsam report for the extension on the phone (checked with pymobiledevice3 crash reports; lzfse stubbed because Smart App Control blocks it).
- App, broadcast name, PC window, shortcut, guide renamed to **Den-Dev Live** (bundle IDs unchanged so settings/key stay). Package: `dist\DenDevLive_Portable.zip`. OBS: re-pick the window "[python.exe]: Den-Dev Live".
- CI 46077753042 OK -> build\DenDevLive.ipa (same as build\iPhoneUSBMirror.ipa).

## Phase 16 - Go Live: no GPU, smooth audio, two mics (2026-11-28)
- Phone log (pymobiledevice3 syslog) during a live: 24x "IOGPUMetalError ... Impacting Interactivity" from CIRenderTask: iOS cancels the extension's GPU work while Mobile Legends uses the GPU -> broken frames, stalls, Facebook errors.
- Compositor rewritten without GPU: VideoToolbox scale/convert + CPU CoreGraphics (rotate, logo, banner, badge, card) + vImage box blur on a 1/10 size copy.
- Audio "cut by cut": audio pieces were dropped from the shared bufferingNewest queue -> audio has its own unbounded queue (video may skip frames). Smoothing buffer 200 ms (cap 700 ms). Auto level (game target RMS 0.12, up to +12 dB; mic 0.10, up to +9.5 dB; fast down / slow up; no clipping) + soft limiter, so ML voice-chat "call mode" does not make the stream quiet.
- Two mics: game and mic are independent inputs in AudioClock; either can stop or change format without muting the other. Teammates' voices are part of the game sound; your voice needs Microphone ON in the broadcast sheet.
- CI 46080680910 OK -> build\DenDevLive.ipa; package rebuilt.

## Phase 17 - Den-Dev Live mic: game volume stays normal (2026-11-28)
- Problem: the Microphone switch in the Start Broadcast sheet records in iOS "call" mode, and iOS then turns all other sound (the game) down.
- Fix: the app records your voice itself (`MicSender`, AVAudioSession playAndRecord, mode default = no voice processing, mixWithOthers + defaultToSpeaker), 48 kHz mono 16-bit, sent to the extension over 127.0.0.1:2398. The extension (`MicReceiver`) mixes it into AudioClock. Recording starts when a live starts and stops when it ends.
- Go Live -> Sound: switch "Den-Dev Live mic (game stays loud)" (on by default). Keep Microphone OFF in the broadcast sheet. If the broadcast mic is ON too, the app mic is used and the sheet mic is ignored (no double voice); without app-mic data for 1 s, the sheet mic is used.
- Limits: the Mobile Legends in-game mic is controlled by ML. When you talk in ML, ML itself switches to call mode (iOS may then lower the game a little); the auto level keeps the stream loud. With the phone speaker the app mic also hears the game: earphones give the cleanest sound.
- CI 46085481285 OK -> build\DenDevLive.ipa (mic permission asked when the app opens). Package rebuilt.

## Phase 18 - Final version 1.0 (2026-11-28)
- Version **1.0**: iPhone app (MARKETING_VERSION 1.0) and PC software (`mirror.py --version`, printed at start).
- README.md (what it does, install, layout, build, privacy) and LICENSE = GPL-3.0 (the PC side uses pymobiledevice3, GPL-3.0); LICENSE.txt is also in the package.
- Guide + START HERE.txt: Go Live with the Den-Dev Live mic (Microphone OFF in the broadcast sheet), Studio, floating comments, two new Go Live problems. Package folder suggestion C:\DenDevLive, the iPhone file is `installers\DenDevLive.ipa`.
- Secret scan of all files and the whole git history: no Apple ID, password, stream key or token. Commits use the GitHub noreply email.
- Fresh-download test of `dist\DenDevLive_Portable.zip` (internet mark, extract, unblock): setup check all OK, `Den-Dev Live 1.0` starts.
- Still to test on the real phone: Go Live with the app mic (game volume, voice, teammates), iPhone Wi-Fi, Android, PiP comments, 30-min stability, OBS window re-pick.

## Phase 18b - Two downloads: iPhone and Android (2026-11-28)
- `dist\DenDevLive_iPhone.zip` (266 MB) = `1 - LAPTOP` (program, SETUP, VB-Cable) + `2 - iPHONE` (DenDevLive.ipa, Sideloadly, How to install on iPhone.txt).
- `dist\DenDevLive_Android.zip` (122 MB) = `1 - LAPTOP` + `2 - ANDROID` (How to connect Android.txt; no app on the phone: scrcpy-server is pushed over adb each time).
- Same laptop program in both. SETUP checks Apple Devices + Sideloadly only in the iPhone download and unblocks the whole package. Start file renamed `2 - Start Den-Dev Live.bat`.
- Tested: both self-tests OK; Android zip extracted fresh -> `Den-Dev Live 1.0` starts.

## Phase 19 - Android app (2026-11-28)
- `android/` = Den-Dev Live for Android, same app as the iPhone one (Kotlin + Jetpack Compose, minSdk 29 / Android 10, target 37):
  - Home: USB / Wi-Fi / Go Live cards (same colors and text as iOS).
  - USB: steps only (the laptop uses adb + scrcpy-server, no app needed); button opens Developer options.
  - Wi-Fi: `MirrorService` = MediaProjection -> H.264 MediaCodec (12 Mbps, 60 fps, long side 1920) + game sound (AudioPlaybackCapture) + mic (AUDIO_MIC), same wire protocol as the iPhone (ports 2345/2346, docs/PROTOCOL.md). Rotation = new encoder size, same capture. The laptop shows "Android app".
  - Go Live: `LiveService` = RootEncoder 2.8.2 (GenericStream, ScreenSource, MixAudioSource = game + mic mixed on the phone, no ducking), RTMP/RTMPS, 30 retries, adaptive bitrate, force-render for still screens, notification buttons Mic / Screen / Stop.
  - Studio: landscape / portrait, fit / fill, logo (Photos), banner (4 colors), LIVE badge + timer, Be right back card (drawn by `StreamLook`, one overlay picture per second).
  - Comments: shared WebView with the same reader script as iOS; floating bubble (drag to any side, snaps), tap = see-through comments + Mic / Screen / Full chat / Stop; tap the game = hide (FLAG_WATCH_OUTSIDE_TOUCH). Comments window lets touches through (alpha <= 0.8).
  - Stream key: AES-GCM with an Android Keystore key.
- Cloud build `.github/workflows/build-android.yml` (ubuntu, Java 21, Gradle 9.7.1, AGP 9.3.2, Kotlin 2.4.10). First build OK: run 46119479238 -> build\DenDevLive.apk (29 MB). Not yet tested on a real phone.
- Signing: temporary (debug) key per build until the user OKs a fixed key in GitHub secrets (DDL_KEYSTORE_B64 / DDL_KEYSTORE_PASSWORD); without it, uninstall the old app before installing a new APK.
- Android download now has `2 - ANDROID\DenDevLive.apk` (zip 133 MB).

## Next
- Real-phone tests: iPhone Wi-Fi (new .ipa), Android USB + Wi-Fi.
- Phase 8b: OBS / TikTok LIVE Studio capture check, 30-min stability test.
- Phase 9: docs.

## Problems / notes
- Git has no author identity yet. The repo will be public, so after `gh auth login` set a **repo-local** identity with the GitHub noreply email (`<id>+<login>@users.noreply.github.com`), then make the first commit (files are staged).

## Phase 19b - Android = same as iPhone + strong USB (2026-11-28)
- Problem seen: the TECNO KL5 on USB-C dropped about 30 times in 1.5 h. Windows logged USB
  re-plug errors ("device not found", VID_0000 descriptor failure); the old scrcpy path
  died on every blink and needed 10-30 s to come back.
- Now the Android app works like the iPhone app on USB too: tap USB -> Start mirroring.
  The laptop reaches the app through `adb forward` (like usbmux), same protocol,
  picture + game sound + your mic. The capture keeps running on the phone when the cable
  blinks; the laptop re-forwards and is back in about 1 s.
- Laptop also: switches from scrcpy to the app as soon as the app starts; moves from
  Wi-Fi to USB when the cable is plugged in (Android app too); restarts adb once if the
  phone vanished after a drop; `adb reconnect offline`; small scrcpy helper only for
  mouse control.
- App: iPhone bitrate rules (USB 20 Mbps; Wi-Fi 8 Mbps start, 12 -> 3 adaptive), same
  drop limits, INFO every 5 s (heat, sound, mic, link, bitrate), wake + Wi-Fi locks,
  MirrorService added to the manifest (was missing). USB / Wi-Fi screens = iPhone screens.
- Laptop window: "phone" wording for both phones; phone heat shown for Android too.
- Build 46128778404 OK -> build/DenDevLive.apk, dist zips rebuilt. Real-phone test pending.
- Not changed (needs a yes): Windows "USB selective suspend" is ON (can cause blinks).

