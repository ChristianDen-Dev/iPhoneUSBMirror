# Wire protocol (iPhone → Windows)

The Broadcast Extension listens on TCP port **2345** on the iPhone
(loopback). The Windows receiver connects to that port **through usbmux**
(Apple Mobile Device service, `127.0.0.1:27015`), so the data travels over the
USB cable. The stream is one-way: iPhone → Windows. The receiver sends nothing.

## Packet format

Every packet = 14-byte header + payload. **All header integers are big-endian.**

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | `type` | packet type (table below) |
| 1 | 1 | `flags` | bit 0 = keyframe (VIDEO_FRAME only) |
| 2 | 8 | `timestamp_us` | presentation time in microseconds (iPhone host clock; same clock for audio and video) |
| 10 | 4 | `length` | payload size in bytes |
| 14 | `length` | payload | |

## Types

| Type | Name | Payload |
|---|---|---|
| 1 | `VIDEO_CONFIG` | `u16 width`, `u16 height` (big-endian, encoded size, portrait), then **Annex-B SPS + PPS** (`00 00 00 01` start codes). Sent before **every keyframe**. |
| 2 | `VIDEO_FRAME` | H.264 Annex-B NAL units for one frame. `flags & 1` = keyframe (IDR). |
| 3 | `AUDIO_PCM` | Raw PCM: **48 kHz, signed 16-bit little-endian, 2 channels, interleaved** (4 bytes per frame). |
| 4 | `ORIENTATION` | `u8` = `CGImagePropertyOrientation` raw value from `RPVideoSampleOrientationKey`. Sent when it changes and when a client connects. |
| 5 | `HEARTBEAT` | empty, once per second |
| 6 | `INFO` | UTF-8 diagnostic text (source buffer size/format, encode size, memory footprint, thermal state, Low Power Mode). Sent when the encoder is (re)created and every ~5 s. Receivers may ignore it. |

### Orientation values

The video is always encoded **as ReplayKit delivers it (portrait buffer)**.
The receiver rotates it for display:

| Value | Name | Receiver rotates the image |
|---|---|---|
| 1 | up | none |
| 3 | down | 180° |
| 6 | right | 90° counter-clockwise |
| 8 | left | 90° clockwise |
| 2,4,5,7 | mirrored variants | (not expected from ReplayKit; treated like 1/3/6/8 without mirroring) |

The 6/8 directions were confirmed on a real iPhone 14 Pro Max (Mobile Legends).
The receiver's `R` key can still flip landscape 180° if ever needed.

## Behaviour

- **One client at a time.** A new connection replaces the old one.
- On connect, the iPhone forces a keyframe (preceded by `VIDEO_CONFIG`) and resends `ORIENTATION`.
- If the screen doesn't change, ReplayKit sends no frames. The iPhone then re-sends the
  last image as a keyframe about once per second, so a new receiver always gets a picture.
- **Frame dropping:** if more than ~1 MB is waiting to be sent, or the encoder is busy,
  new video frames are skipped *before* encoding (so the H.264 stream stays valid).
  Audio is only dropped if more than 2 MB is backed up.
- Video: H.264 High profile, no B-frames, ~20 Mbps average (30 Mbps cap), keyframe ≥ every 2 s,
  long side ≤ 1920 px (iPhone 14 Pro Max: 1290×2796 → 886×1920).


## Sound-only channel (port 2346)

Newer iPhone builds also listen on **127.0.0.1:2346**. When a receiver is
connected there, AUDIO_PCM packets (same 14-byte header) are sent ONLY on that
connection, so sound never waits behind large video frames on the phone. The
receiver connects it after the main connection is up; if the port is closed
(older app), sound keeps arriving on the main connection (2345). Heartbeats
(type 5) are also sent there every second.
