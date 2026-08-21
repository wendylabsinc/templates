import Foundation
import NIOCore

// ───────────────────────────────────────────────────────────────────────────
// Wire format
//
// Specified in Doc/PROTOCOL.md, which is the reference for message layouts,
// field semantics and error handling. The constants below must agree with it.
//
// That file is a copy of the one in xiao-esp32s3-camera-stream, the device
// repository, which holds the source of truth; the device side reads the same
// constants out of it in main/server.h.
// ───────────────────────────────────────────────────────────────────────────

let kMagic: UInt8 = 0xAF
let kHeaderLength = 4
let kMaxMessageLength = 1_408

// Type 0 is the handshake, which is optional and which this client does not
// send. Types 1 and 2 are reserved.
let kMessageTypeRequest: UInt8 = 3
let kMessageTypeData: UInt8 = 4

let kChannelVideo: UInt8 = 1

let kRequestPayloadLength = 12
let kDataHeaderLength = 20

// The top bit of the 24 bit word at offset 1 of a data payload.
let kLastChunkFlag: UInt32 = 0x80_0000
let kChunkNumberMask: UInt32 = 0x7F_FFFF

/// One framed message off the wire. The payload excludes the header.
struct AVMessage: Sendable {
    let type: UInt8
    var payload: ByteBuffer
}

// ───────────────────────────────────────────────────────────────────────────
// Decoder
// ───────────────────────────────────────────────────────────────────────────

// A ByteToMessageDecoder rather than hand-rolled buffering in the read loop:
// NIO already handles the partial-read bookkeeping, and a frame arrives as
// dozens of small messages so getting that wrong would be expensive.
struct AVMessageDecoder: ByteToMessageDecoder {
    typealias InboundOut = AVMessage

    mutating func decode(context: ChannelHandlerContext, buffer: inout ByteBuffer) throws -> DecodingState {
        guard let header = buffer.getBytes(at: buffer.readerIndex, length: kHeaderLength) else {
            return .needMoreData
        }
        guard header[0] == kMagic else {
            throw AVSourceError.invalidMagic(header[0])
        }

        let payloadLength = Int(header[2]) << 8 | Int(header[3])
        guard payloadLength <= kMaxMessageLength - kHeaderLength else {
            // Nothing we can resynchronize on, the stream is lost.
            throw AVSourceError.payloadTooLarge(payloadLength)
        }
        guard buffer.readableBytes >= kHeaderLength + payloadLength else {
            return .needMoreData
        }

        buffer.moveReaderIndex(forwardBy: kHeaderLength)
        let payload = buffer.readSlice(length: payloadLength)!
        context.fireChannelRead(wrapInboundOut(AVMessage(type: header[1], payload: payload)))
        return .continue
    }
}

// ───────────────────────────────────────────────────────────────────────────
// Encoder
// ───────────────────────────────────────────────────────────────────────────

// Builds a type 3 message asking for a single video frame. The device ignores
// the frame count and the delay today, so they are pinned at 1 and 0.
func makeVideoRequest(requestID: UInt32, allocator: ByteBufferAllocator) -> ByteBuffer {
    var buffer = allocator.buffer(capacity: kHeaderLength + kRequestPayloadLength)
    buffer.writeInteger(kMagic)
    buffer.writeInteger(kMessageTypeRequest)
    buffer.writeInteger(UInt16(kRequestPayloadLength))
    buffer.writeInteger(kChannelVideo)
    buffer.writeInteger(UInt8(1))
    buffer.writeInteger(UInt16(0))
    buffer.writeInteger(UInt32(0))
    buffer.writeInteger(requestID)
    return buffer
}
