import Foundation
import NIOCore
import NIOPosix

/// AVSource — connects to a wendy-lite AV source and pulls video frames.
///
/// The device answers one request with one frame, and queues one request
/// behind the one it is serving, so the loop keeps two requests pending: the
/// next capture starts the moment the current frame finishes going out, rather
/// than waiting on our reassembly, our handlers, and a request round trip.
public actor AVSource {
    public static let defaultPort = 3_333

    // The device serves one request and queues at most one more, so two in
    // flight keeps it busy end to end. A third would just park in a buffer.
    private static let pendingRequestCount = 2

    private let host: String
    private let port: Int

    private var handlers: [ObjectIdentifier: @Sendable (VideoFrame) async -> Void] = [:]
    private var channel: NIOAsyncChannel<AVMessage, ByteBuffer>?
    private var streamTask: Task<Void, Never>?
    private var nextRequestID: UInt32 = 1

    // True from the moment start() has a live connection until the stream loop
    // ends, whichever way it ends: stop(), the device going away, or a decode
    // error. Callers poll this to notice a source that died on its own.
    public private(set) var isConnected = false

    public init(host: String, port: Int = AVSource.defaultPort) {
        self.host = host
        self.port = port
    }

    // Registers a closure called once per complete frame. It can be called
    // several times; every registered closure sees every frame. Hand the
    // returned token to unsubscribe(_:) to drop just that closure.
    public func subscribeToVideoFrames(_ handler: @escaping @Sendable (VideoFrame) async -> Void) -> AVSourceSubscription {
        let token = AVSourceSubscription()
        handlers[ObjectIdentifier(token)] = handler
        return token
    }

    public func unsubscribe(_ token: AVSourceSubscription) {
        handlers.removeValue(forKey: ObjectIdentifier(token))
    }

    // Connects, then leaves the request/receive loop running in the
    // background. Connection failures surface from here rather than getting
    // swallowed by the loop.
    public func start() async throws {
        guard streamTask == nil else {
            throw AVSourceError.alreadyRunning
        }

        let connected = try await ClientBootstrap(group: MultiThreadedEventLoopGroup.singleton)
            .connectTimeout(.seconds(10))
            .channelOption(.tcpOption(.tcp_nodelay), value: 1)
            .connect(host: host, port: port) { channel in
                channel.eventLoop.makeCompletedFuture {
                    try channel.pipeline.syncOperations.addHandler(ByteToMessageHandler(AVMessageDecoder()))
                    return try NIOAsyncChannel<AVMessage, ByteBuffer>(
                        wrappingChannelSynchronously: channel,
                    )
                }
            }

        channel = connected
        isConnected = true
        print("[avsource] connected to \(host):\(port)")

        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.stream(over: connected)
            } catch is CancellationError {
                // Falls through rather than returning: every exit from the loop
                // has to clear isConnected, and a defer can't await.
            } catch {
                print("[avsource] stream ended: \(error)")
            }
            await self.markDisconnected()
        }
    }

    // Cancels the loop and closes the connection, returning only once the
    // loop has actually finished.
    public func stop() async {
        streamTask?.cancel()
        if let channel {
            try? await channel.channel.close()
        }
        await streamTask?.value
        streamTask = nil
        channel = nil
        isConnected = false
    }

    //=== private ===//

    private func markDisconnected() {
        isConnected = false
    }

    private func stream(over channel: NIOAsyncChannel<AVMessage, ByteBuffer>) async throws {
        try await channel.executeThenClose { inbound, outbound in
            var assembler = FrameAssembler()

            // Request IDs in the order they went out. TCP keeps frames in that
            // same order, so the head is the one the next frame should carry.
            var pending: [UInt32] = []
            while pending.count < Self.pendingRequestCount {
                pending.append(try await self.requestFrame(on: outbound))
            }

            for try await message in inbound {
                if Task.isCancelled { return }

                // Anything else is a type we do not handle — a reserved one, a
                // handshake answer — and PROTOCOL.md has us read on past it.
                guard message.type == kMessageTypeData else { continue }

                guard let frame = try assembler.accept(message) else { continue }
                // A device with no room for a request discards it silently, so
                // a frame can answer one further down the queue. Everything
                // ahead of the match went unanswered and will never come.
                if let index = pending.firstIndex(of: frame.requestID) {
                    if index > 0 {
                        print("[avsource] \(index) request(s) dropped by the device before frame \(frame.number)")
                    }
                    pending.removeFirst(index + 1)
                } else {
                    // A frame we never asked for, or one for a request already
                    // accounted for: worth a line in the log, not worth
                    // trapping over, and nothing to remove from the queue.
                    print("[avsource] frame \(frame.number) answers unknown request 0x\(String(frame.requestID, radix: 16))")
                }

                // Refills before handing the frame out: waiting until the
                // handlers return would leave the device idle for exactly as
                // long as they take.
                while pending.count < Self.pendingRequestCount {
                    pending.append(try await self.requestFrame(on: outbound))
                }

                if Task.isCancelled { return }
                await self.broadcast(frame)
            }

            throw AVSourceError.connectionClosed
        }
    }

    private func requestFrame(on outbound: NIOAsyncChannelOutboundWriter<ByteBuffer>) async throws -> UInt32 {
        let requestID = nextRequestID
        nextRequestID &+= 1
        try await outbound.write(makeVideoRequest(requestID: requestID, allocator: ByteBufferAllocator()))
        return requestID
    }

    private func broadcast(_ frame: VideoFrame) async {
        let handlers = Array(self.handlers.values)
        await withTaskGroup(of: Void.self) { group in
            for handler in handlers {
                group.addTask { await handler(frame) }
            }
        }
    }
}

// Opaque identity for one registered closure, so a caller can drop its own
// subscription without the module handing out array indices.
public final class AVSourceSubscription: Sendable {
    public init() {}
}
