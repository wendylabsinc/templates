import CTurboJPEG
import RealSenseKit

// TJFLAG_FASTDCT is a #define and doesn't import into Swift.
private let tjFlagFastDCT: Int32 = 2048

enum JPEGEncoder {
    static let quality: Int32 = 80

    // Encodes one frame view with TurboJPEG. Must run on the capture thread,
    // while the view's backing frameset is still alive (i.e. before the next
    // cameraWaitForFrames call). Returns nil on encode failure.
    static func encode(_ view: rsk.FrameView, encoder: OpaquePointer?) -> [UInt8]? {
        guard view.isValid(), let encoder else { return nil }

        let pixelFormat: Int32
        let subsampling: Int32
        switch view.format {
        case .bgr8:
            pixelFormat = Int32(TJPF_BGR.rawValue)
            subsampling = Int32(TJSAMP_420.rawValue)
        case .y8:
            pixelFormat = Int32(TJPF_GRAY.rawValue)
            subsampling = Int32(TJSAMP_GRAY.rawValue)
        case .rgb8:
            pixelFormat = Int32(TJPF_RGB.rawValue)
            subsampling = Int32(TJSAMP_420.rawValue)
        }

        var jpegBuffer: UnsafeMutablePointer<UInt8>? = nil
        var jpegSize: UInt = 0
        let rc = tjCompress2(
            encoder, view.data, Int32(view.width), 0, Int32(view.height),
            pixelFormat, &jpegBuffer, &jpegSize, subsampling, quality, tjFlagFastDCT
        )
        guard rc == 0, let buffer = jpegBuffer else {
            if let buffer = jpegBuffer { tjFree(buffer) }
            return nil
        }
        defer { tjFree(buffer) }
        return Array(UnsafeBufferPointer(start: buffer, count: Int(jpegSize)))
    }
}
