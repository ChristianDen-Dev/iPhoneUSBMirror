// Converts ReplayKit `audioApp` buffers (often 44.1 kHz, 16-bit, and
// sometimes BIG-endian) into 48 kHz, signed 16-bit little-endian, stereo,
// interleaved PCM: exactly what the Windows receiver plays.
//
// Step 1: read any linear-PCM layout by hand into Float32 stereo
//         (handles int16/int32/float32, big/little endian, mono/stereo,
//         interleaved/non-interleaved).
// Step 2: AVAudioConverter resamples to 48 kHz and converts to Int16.

import Foundation
import AVFoundation
import CoreMedia

final class AppAudioConverter {
    static let outRate: Double = 48_000

    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                          sampleRate: AppAudioConverter.outRate,
                                          channels: 2, interleaved: true)!
    private var floatFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private var currentRate: Double = 0

    /// Returns PCM bytes ready to send, or nil if the buffer can't be used.
    func convert(_ sb: CMSampleBuffer) -> Data? {
        guard let fd = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fd) else { return nil }
        let asbd = asbdPtr.pointee
        guard asbd.mFormatID == kAudioFormatLinearPCM, asbd.mSampleRate > 0,
              asbd.mChannelsPerFrame > 0 else { return nil }

        let frames = CMSampleBufferGetNumSamples(sb)
        guard frames > 0 else { return nil }

        // (Re)build the converter when the input sample rate changes.
        if converter == nil || asbd.mSampleRate != currentRate {
            guard let ff = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: asbd.mSampleRate,
                                         channels: 2, interleaved: false),
                  let conv = AVAudioConverter(from: ff, to: outFormat) else { return nil }
            floatFormat = ff
            converter = conv
            currentRate = asbd.mSampleRate
        }
        guard let floatFormat = floatFormat, let converter = converter,
              let inBuf = AVAudioPCMBuffer(pcmFormat: floatFormat, frameCapacity: AVAudioFrameCount(frames)),
              let dst = inBuf.floatChannelData else { return nil }
        inBuf.frameLength = AVAudioFrameCount(frames)

        // Step 1: decode raw samples into Float32 L/R.
        let ok: Bool = (try? sb.withAudioBufferList { abl, _ -> Bool in
            Self.readSamples(abl, asbd: asbd, frames: frames, left: dst[0], right: dst[1])
        }) ?? false
        guard ok else { return nil }

        // Step 2: resample + convert to Int16 interleaved.
        let outCapacity = AVAudioFrameCount(Double(frames) * Self.outRate / asbd.mSampleRate) + 64
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: outCapacity) else { return nil }
        var fed = false
        var error: NSError?
        let status = converter.convert(to: outBuf, error: &error) { _, inputStatus in
            if fed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            fed = true
            inputStatus.pointee = .haveData
            return inBuf
        }
        guard status != .error, outBuf.frameLength > 0,
              let mData = outBuf.audioBufferList.pointee.mBuffers.mData else { return nil }

        // Int16 interleaved stereo = 4 bytes per frame; iOS is little-endian.
        return Data(bytes: mData, count: Int(outBuf.frameLength) * 4)
    }

    private static func readSamples(_ abl: UnsafeMutableAudioBufferListPointer,
                                    asbd: AudioStreamBasicDescription,
                                    frames: Int,
                                    left: UnsafeMutablePointer<Float>,
                                    right: UnsafeMutablePointer<Float>) -> Bool {
        let flags = asbd.mFormatFlags
        let isFloat = flags & kAudioFormatFlagIsFloat != 0
        let isBigEndian = flags & kAudioFormatFlagIsBigEndian != 0
        let nonInterleaved = flags & kAudioFormatFlagIsNonInterleaved != 0
        let bits = Int(asbd.mBitsPerChannel)
        let channels = Int(asbd.mChannelsPerFrame)
        let bytesPerSample = bits / 8
        guard bits == 16 || bits == 32, abl.count > 0 else { return false }

        func sample(_ base: UnsafeRawPointer, _ index: Int) -> Float {
            let off = index * bytesPerSample
            if bits == 16 {
                let raw = base.loadUnaligned(fromByteOffset: off, as: UInt16.self)
                let v = isBigEndian ? UInt16(bigEndian: raw) : UInt16(littleEndian: raw)
                return Float(Int16(bitPattern: v)) / 32768.0
            }
            let raw = base.loadUnaligned(fromByteOffset: off, as: UInt32.self)
            let v = isBigEndian ? UInt32(bigEndian: raw) : UInt32(littleEndian: raw)
            return isFloat ? Float(bitPattern: v) : Float(Int32(bitPattern: v)) / 2_147_483_648.0
        }

        for (outIndex, out) in [left, right].enumerated() {
            let srcChannel = min(outIndex, channels - 1)   // mono -> both sides
            if nonInterleaved {
                guard srcChannel < abl.count, let base = abl[srcChannel].mData,
                      Int(abl[srcChannel].mDataByteSize) >= frames * bytesPerSample else { return false }
                for i in 0..<frames { out[i] = sample(UnsafeRawPointer(base), i) }
            } else {
                guard let base = abl[0].mData,
                      Int(abl[0].mDataByteSize) >= frames * channels * bytesPerSample else { return false }
                for i in 0..<frames { out[i] = sample(UnsafeRawPointer(base), i * channels + srcChannel) }
            }
        }
        return true
    }
}
