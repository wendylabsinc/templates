// std_msgs/String subscriber on /chatter — the Swift half of the ROS 2
// demo_nodes talker/listener pair, over CycloneDDS (ROS 2 Humble wire format).
import Logging
import OTel
import ServiceLifecycle
import SwiftROS2

struct Listener: Service {
    let logger: Logger

    func run() async throws {
        let ctx = try await ROS2Context(
            transport: .ddsMulticast(domainId: {{.ROS_DOMAIN_ID}}),
            distro: .humble
        )
        let node = try await ctx.createNode(name: "listener")
        let subscription = try await node.createSubscription(StringMsg.self, topic: "chatter")

        logger.info("Listening for ROS 2 messages", metadata: ["ros.topic": "/chatter"])
        for await message in subscription.messages.cancelOnGracefulShutdown() {
            logger.info(
                "Received ROS 2 message",
                metadata: ["ros.topic": "/chatter", "message.body": "\(message.data)"]
            )
        }
        await ctx.shutdown()
    }
}

@main
struct ListenerApp {
    static func main() async throws {
        let observability = try OTel.bootstrap()
        let logger = Logger(label: "{{.APP_ID}}-listener")
        try await ServiceGroup(
            services: [observability, Listener(logger: logger)],
            gracefulShutdownSignals: [.sigterm, .sigint],
            logger: logger
        ).run()
    }
}
