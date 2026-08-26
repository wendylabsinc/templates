import Foundation

/// Collect data chunks from a stream and emit complete JPEG frames.
struct JPEGFrameParser: Sendable {
    private static let maxBuffered = 10_000_000

    private var buffer = Data(capacity: maxBuffered)
    // Index of the SOI marker, once found.
    private var soi: Int?
    // First index not yet examined as the *first* byte of a marker pair.
    private var scanned = 0

    mutating func append(_ data: Data) -> [Data] {
        // Cap before append so a malformed source can't grow the buffer past
        // the limit before the next reset.
        if buffer.count + data.count > Self.maxBuffered { reset() }
        buffer.append(data)

        var frames: [Data] = []
        while true {
            if soi == nil {
                guard let start = marker(0xD8, from: scanned) else {
                    // Nothing up to the final byte can open a frame, so drop it
                    // all; keep the last byte in case it is a leading 0xFF.
                    drop(through: buffer.endIndex - 2)
                    scanned = buffer.startIndex
                    break
                }
                soi = start
                scanned = start + 2  // EOI can't overlap SOI
            }
            guard let end = marker(0xD9, from: scanned) else {
                // A pair can't open at the last byte, so resume there: that is
                // one byte before whatever arrives next.
                scanned = max(scanned, buffer.endIndex - 1)
                break
            }
            frames.append(Data(buffer[soi!...(end + 1)]))
            drop(through: end + 1)
            soi = nil
            scanned = buffer.startIndex
        }
        return frames
    }

    /// First index >= `from` holding the pair `0xFF, second`.
    private func marker(_ second: UInt8, from: Int) -> Int? {
        let origin = buffer.startIndex
        let from = max(from, origin)
        guard buffer.endIndex - from >= 2 else { return nil }
        let bytes = buffer.span
        let limit = bytes.count - 1  // last index that can open a pair
        var i = from - origin
        while i < limit {
            if bytes[i] == 0xFF && bytes[i + 1] == second { return i + origin }
            i += 1
        }
        return nil
    }

    private mutating func drop(through index: Int) {
        guard index >= buffer.startIndex else { return }
        buffer.removeSubrange(buffer.startIndex...index)
    }

    // Clears the scan state too: a stale `soi` past the end of an emptied
    // buffer would trap on the next emit.
    private mutating func reset() {
        buffer.removeAll(keepingCapacity: true)
        soi = nil
        scanned = 0
    }
}
