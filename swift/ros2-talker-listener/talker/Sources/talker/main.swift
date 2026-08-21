// std_msgs/String publisher on /chatter at 1 Hz — the Swift half of the ROS 2
// demo_nodes talker/listener pair, over CycloneDDS (ROS 2 Humble wire format).
import Logging
import OTel
import ServiceLifecycle
import SwiftROS2

struct Talker: Service {
    let logger: Logger

    func run() async throws {
        let ctx = try await ROS2Context(
            transport: .ddsMulticast(domainId: {{.ROS_DOMAIN_ID}}),
            distro: .humble
        )
        let node = try await ctx.createNode(name: "talker")
        let publisher = try await node.createPublisher(StringMsg.self, topic: "chatter")

        let publishingTask = Task {
            var count = 0
            while !Task.isCancelled {
                count += 1
                let message = StringMsg(data: "Hello World: \(count)")
                try publisher.publish(message)
                logger.info(
                    "Published ROS 2 message",
                    metadata: [
                        "ros.topic": "/chatter",
                        "message.body": "\(message.data)",
                        "message.sequence": "\(count)",
                    ]
                )
                try await Task.sleep(for: .seconds(1))
            }
        }

        do {
            try await withTaskCancellationOrGracefulShutdownHandler {
                try await publishingTask.value
            } onCancelOrGracefulShutdown: {
                publishingTask.cancel()
            }
        } catch is CancellationError {
            // Graceful shutdown continues below.
        } catch {
            await ctx.shutdown()
            throw error
        }
        await ctx.shutdown()
    }
}

@main
struct TalkerApp {
    static func main() async throws {
        let observability = try OTel.bootstrap()
        let logger = Logger(label: "{{.APP_ID}}-talker")
        try await ServiceGroup(
            services: [observability, Talker(logger: logger)],
            gracefulShutdownSignals: [.sigterm, .sigint],
            logger: logger
        ).run()
    }
}
