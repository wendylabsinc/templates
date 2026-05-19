internal import Foundation

struct JPEGFrameParser: Sendable {
    private var buffer = Data()

    mutating func append(_ data: Data) -> [Data] {
        buffer.append(data)
        var frames: [Data] = []

        while let range = findFrame() {
            frames.append(Data(buffer[range]))
            buffer.removeSubrange(buffer.startIndex...range.upperBound)
        }

        if buffer.count > 10_000_000 {
            buffer.removeAll()
        }

        return frames
    }

    private func findFrame() -> ClosedRange<Int>? {
        guard buffer.count >= 4 else { return nil }

        var soi: Int?
        for i in buffer.startIndex..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD8 {
                soi = i
                break
            }
        }
        guard let start = soi else { return nil }

        for i in (start + 2)..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD9 {
                return start...(i + 1)
            }
        }
        return nil
    }
}
