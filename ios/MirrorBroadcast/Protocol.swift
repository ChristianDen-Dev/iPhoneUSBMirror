// Wire protocol (see docs/PROTOCOL.md). All integers are big-endian.
//
//   type (u8) | flags (u8) | timestamp_us (u64) | length (u32) | payload[length]
//   = 14-byte header

import Foundation
import CoreMedia

enum PacketType: UInt8 {
    case videoConfig = 1   // u16 width, u16 height, then Annex-B SPS + PPS
    case videoFrame  = 2   // Annex-B NAL units (00 00 00 01 start codes)
    case audioPCM    = 3   // 48 kHz, s16 little-endian, stereo interleaved
    case orientation = 4   // u8 CGImagePropertyOrientation raw value (1...8)
    case heartbeat   = 5   // empty
    case info        = 6   // UTF-8 diagnostic text (receiver prints it)
}

enum PacketFlags {
    static let keyframe: UInt8 = 0x01   // on videoFrame
}

enum Packet {
    static let headerSize = 14

    static func make(_ type: PacketType, flags: UInt8 = 0, timestampUs: UInt64, payload: Data) -> Data {
        var d = Data(capacity: headerSize + payload.count)
        d.append(type.rawValue)
        d.append(flags)
        appendBE(&d, timestampUs)
        appendBE(&d, UInt32(payload.count))
        d.append(payload)
        return d
    }

    static func appendBE<T: FixedWidthInteger>(_ d: inout Data, _ value: T) {
        var be = value.bigEndian
        withUnsafeBytes(of: &be) { d.append(contentsOf: $0) }
    }
}

/// Physical memory footprint of this process in MB (what the ~50 MB limit counts).
func memoryFootprintMB() -> Double {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    let kr = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
        }
    }
    return kr == KERN_SUCCESS ? Double(info.phys_footprint) / 1_048_576 : -1
}

/// FourCC pixel format as text, e.g. "420f".
func fourCC(_ v: OSType) -> String {
    let bytes = [UInt8((v >> 24) & 0xff), UInt8((v >> 16) & 0xff), UInt8((v >> 8) & 0xff), UInt8(v & 0xff)]
    return String(bytes: bytes, encoding: .ascii) ?? String(v)
}

/// CMTime -> microseconds (host clock). Negative / invalid -> 0.
func microseconds(_ t: CMTime) -> UInt64 {
    guard t.isValid, t.isNumeric else { return 0 }
    let s = CMTimeGetSeconds(t)
    return s > 0 ? UInt64(s * 1_000_000) : 0
}
