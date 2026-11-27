# iPhone USB Mirror â€” Progress Log

Read this first when resuming work.

## Status

| Phase | Name | Status |
|---|---|---|
| 0 | Check computer | âœ… Done (2026-11-27) |
| 1 | Create project | âœ… Done (2026-11-27) |
| 2 | Test USB connection | â³ Next |
| 3 | iPhone app (Swift) | â€” |
| 4 | Cloud build (GitHub Actions) | â€” |
| 5 | Install with Sideloadly | â€” |
| 6 | Windows receiver (video) | â€” |
| 7 | Game audio | â€” |
| 8 | Polish + OBS | â€” |
| 9 | Documentation | â€” |

## Phase 0 â€” Computer check (2026-11-27)
- Git 2.56.0 âœ…
- Python 3.14.8 64-bit âœ… (only version installed; watch for missing package wheels)
- pip 26.2.1 âœ…
- GitHub CLI 2.106.0 âœ… (`C:\Program Files\GitHub CLI\gh.exe`) â€” **not logged in yet** (`gh auth login` needed before Phase 4)
- Apple Devices app 1.1792 âœ… â€” usbmux runs as `AppleMobileDeviceProcess.exe` on `127.0.0.1:27015` (not a Windows service; that is normal for the Apple Devices app)
- Sideloadly âœ… (`%LOCALAPPDATA%\Sideloadly`)
- OBS Studio 33.0.3 âœ…

## Phase 1 â€” Project created (2026-11-27)
- Folder layout: `ios\`, `receiver\`, `.github\workflows\`, `docs\`, `build\` (ignored)
- Git repo initialized on branch `main`, `.gitignore` added
- Python venv: `receiver\.venv`

## Phase 2 â€” USB connection âœ… (2026-11-27)
- Installed in venv: pymobiledevice3 11.66.1, av 19.0.2, numpy 2.5.5, pygame-ce 2.5.8, sounddevice 0.5.6 (all have Python 3.14 wheels) â†’ `receiver\requirements.txt`
- Note: pymobiledevice3 11.x is **async** (`await usbmux.list_devices()`, `await MuxDevice.connect(port)` returns a socket).
- `receiver\check_device.py` â†’ **iPhone detected**: iPhone 14 Pro Max, iOS 27.0 (24A437) (UDID kept out of this public repo).
- **Problem fixed:** the iPhone was bound to a `libusb-win32` driver (libwdi/Zadig-generated `oem58.inf`, installed 2026-11-27 08:00), so usbmux only saw it over Network. Removed it with `pnputil /delete-driver oem58.inf /uninstall` (user approved) â†’ Apple driver `appleusb.inf` (oem62.inf) took over â†’ replug â†’ detected over USB. If any tool reinstalls libusb for the iPhone, mirroring will break again.

## Phase 3 â€” iPhone app âœ… written (2026-11-27, compile check happens in Phase 4)
- `ios\project.yml`: XcodeGen, iOS 16.0, app `com.usbmirror.iphoneusbmirror` + extension `â€¦.MirrorBroadcast`, Swift 5 mode, shared scheme.
- `ios\App\MirrorApp.swift`: SwiftUI screen + big `RPSystemBroadcastPickerView` (extension ID looked up at runtime, since Sideloadly may rename IDs).
- `ios\MirrorBroadcast\`:
  - `Protocol.swift`: 14-byte big-endian header, packet types 1â€“5
  - `MirrorServer.swift`: NWListener on 127.0.0.1:2345 (falls back to any-address), newest client wins, backlog tracking, 1 s heartbeat
  - `VideoEncoder.swift`: VTCompressionSession H.264 Main, real-time, no reordering, 8 Mbps / 12 Mbps cap, keyframe â‰¤1 s, downscale to long side 1920 (886Ã—1920) via VTPixelTransferSession, SPS/PPS before every keyframe, idle-repeat keyframe
  - `AudioPCM.swift`: audioApp â†’ Float32 (handles big-endian/int/float/mono) â†’ AVAudioConverter â†’ 48 kHz s16le stereo
  - `SampleHandler.swift`: wires it all; drops video if encoder busy or backlog >300 KB; no encoding when no client
- `docs\PROTOCOL.md` written.
- `.github\workflows\build-ios.yml` written (Phase 4).

## Phase 4 â€” Cloud build âœ… (2026-11-27)
- GitHub account `ChristianDen-Dev`; gh token scopes include `workflow` (needed to push workflow files).
- Repo-local git identity uses the GitHub noreply email (personal email never committed).
- Public repo: https://github.com/ChristianDen-Dev/iPhoneUSBMirror
- First build (run 46028002248) passed in 32 s on Xcode 26.6: no Swift errors/warnings; extension Info.plist verified inside the .ipa.
- Added `UIRequiresFullScreen` to silence the portrait-only warning.
- `.ipa` downloaded to `build\iPhoneUSBMirror.ipa` (git-ignored).
- Re-download a fresh build: `gh run download <run-id> --name iPhoneUSBMirror-ipa --dir build`

## Phase 5 â€” Install âœ… (2026-11-27)
- Installed with Sideloadly (free Apple ID). Sideloadly renamed the bundle ID to `com.usbmirror.iphoneusbmirror.23KRTS7HNT` (handled: the app finds its extension at runtime).
- Needed: Developer Mode ON + trust the developer profile (VPN & Device Management).

## Phase 6 â€” Video receiver âœ… (2026-11-28)
- `receiver\mirror.py` (pygame-ce window "iPhone USB Mirror", PyAV decode, aspect-fit, F11/Esc/F1/R, auto-reconnect, console stats every 5 s).
- `receiver\tools\fake_iphone.py`: protocol-exact fake sender (libx264 + test tone) for testing without the phone (`mirror.py --tcp 127.0.0.1:2345`).
- **Smart App Control** blocked `av\format.pyd` of PyAV 19.0.2 â†’ pinned **PyAV 19.0.1** (yanked only for an audio-decoding bug we don't use). SAC left ON (turning it off is irreversible).
- Bug found by fake test and fixed: non-contiguous numpy array in portrait.
- **Real iPhone test:** works over USB; Mobile Legends at a steady **60 fps, ~9.5 Mbps, decode 7â€“13 ms**. Landscape was upside-down â†’ 6/8 rotation swapped (now verified correct).
- **Open issue:** in Mobile Legends the stream is only **408Ã—886** (portrait home screen is 886Ã—1918) â†’ looks blurry when enlarged. Cause unknown: the iPhone hands the extension a smaller buffer. Added an `INFO` packet (type 6) reporting source buffer size/format, encode size, memory, thermal state, Low Power Mode â†’ needs re-install of the new build (run 46034288497) to diagnose.
- The extension's NSLog lines are not visible via `pymobiledevice3 syslog live` (only ReplayKit/system lines are), hence the INFO packet.

## Phase 7 â€” Game audio âœ… (2026-11-28)
- `receiver\audio_player.py`: sounddevice RawOutputStream 48 kHz s16 stereo, 5 ms blocks, 80 ms jitter buffer, re-buffer on underrun, skip-ahead above 200 ms (no delay build-up). `M` = mute.
- Fake-tone test and real iPhone: plays on "Speakers (Realtek(R) Audio)", 0 underruns, 0 skips.
- Still to confirm by the user: game sound heard; OBS "Desktop Audio" picks it up (Phase 8).

## Next
- Re-install new .ipa (diagnostics) â†’ read INFO line in ML â†’ fix sharpness.
- Phase 8: start_mirror.bat, OBS setup, 30-min stability test.

## Problems / notes
- Git has no author identity yet. The repo will be public, so after `gh auth login` set a **repo-local** identity with the GitHub noreply email (`<id>+<login>@users.noreply.github.com`), then make the first commit (files are staged).
