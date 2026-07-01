// std_msgs/String subscriber on /chatter — the Swift half of the ROS 2
// demo_nodes talker/listener pair, over CycloneDDS (ROS 2 Humble wire format).
import Foundation
import SwiftROS2

// Write straight to stdout so `wendy device logs` shows each line live —
// Swift's `print` block-buffers when stdout isn't a terminal.
func log(_ message: String) {
    FileHandle.standardOutput.write(Data((message + "\n").utf8))
}

let ctx = try await ROS2Context(
    transport: .ddsMulticast(domainId: {{.ROS_DOMAIN_ID}}),
    distro: .humble
)
let node = try await ctx.createNode(name: "listener")
let sub = try await node.createSubscription(StringMsg.self, topic: "chatter")

log("Listening on /chatter...")
for await msg in sub.messages {
    log("I heard: '\(msg.data)'")
}

await ctx.shutdown()
