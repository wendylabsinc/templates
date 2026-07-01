// std_msgs/String publisher on /chatter at 1 Hz — the Swift half of the ROS 2
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
let node = try await ctx.createNode(name: "talker")
let pub = try await node.createPublisher(StringMsg.self, topic: "chatter")

var count = 0
while !Task.isCancelled {
    count += 1
    let msg = StringMsg(data: "Hello World: \(count)")
    try pub.publish(msg)
    log("Publishing: '\(msg.data)'")
    try await Task.sleep(nanoseconds: 1_000_000_000)
}

await ctx.shutdown()
