import Foundation

/// One complete video frame, reassembled from the chunks the device sent for a
/// single request.
public struct VideoFrame: Sendable {
    public let channel: UInt8
    public let number: UInt32
    /// Device clock, microseconds since it booted.
    public let timestamp: UInt32
    /// Mirrors `timestamp` until the two clocks are synchronized.
    public let hostTimestamp: UInt32
    /// The id of the request this frame answers.
    public let requestID: UInt32
    public let jpeg: Data
}

/// A typed error enum rather than the NSError the rest of the package throws:
/// this is a library boundary, so callers need something to switch on.
public enum AVSourceError: Error, Sendable {
    case invalidMagic(UInt8)
    case payloadTooLarge(Int)
    case malformedFrame
    case unexpectedChannel(UInt8)
    case unexpectedChunk(expected: UInt32, got: UInt32)
    case connectionClosed
    case alreadyRunning
}
