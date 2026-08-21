import Foundation
import NIOCore

/// Collects the run of type 4 messages that make up one frame. Chunks travel
/// over TCP so they cannot arrive out of order or be dropped silently: a gap
/// means the stream is out of sync, which is worth surfacing rather than
/// papering over with a partial image.
///
/// Takes data messages only; the caller filters the types it does not handle.
struct FrameAssembler: Sendable {
    private var expectedChunk: UInt32 = 0
    private var jpeg = Data()

    mutating func accept(_ message: AVMessage) throws -> VideoFrame? {
        // A payload too short for the layout its type calls for is ignored,
        // like a message of an unknown type, per PROTOCOL.md.
        guard message.payload.readableBytes >= kDataHeaderLength else {
            return nil
        }

        var payload = message.payload
        guard let channel: UInt8 = payload.readInteger(),
              let high: UInt8 = payload.readInteger(),
              let mid: UInt8 = payload.readInteger(),
              let low: UInt8 = payload.readInteger(),
              let number: UInt32 = payload.readInteger(),
              let timestamp: UInt32 = payload.readInteger(),
              let hostTimestamp: UInt32 = payload.readInteger(),
              let requestID: UInt32 = payload.readInteger()
        else {
            throw AVSourceError.malformedFrame
        }
        guard channel == kChannelVideo else {
            throw AVSourceError.unexpectedChannel(channel)
        }

        let word = UInt32(high) << 16 | UInt32(mid) << 8 | UInt32(low)
        let chunk = word & kChunkNumberMask
        let isLast = word & kLastChunkFlag != 0

        guard chunk == expectedChunk else {
            reset()
            throw AVSourceError.unexpectedChunk(expected: expectedChunk, got: chunk)
        }

        if let bytes = payload.readBytes(length: payload.readableBytes) {
            jpeg.append(contentsOf: bytes)
        }
        expectedChunk += 1

        guard isLast else { return nil }

        let frame = VideoFrame(
            channel: channel,
            number: number,
            timestamp: timestamp,
            hostTimestamp: hostTimestamp,
            requestID: requestID,
            jpeg: jpeg,
        )
        reset()
        return frame
    }

    mutating func reset() {
        expectedChunk = 0
        jpeg = Data()
    }
}
