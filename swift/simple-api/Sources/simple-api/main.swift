import Foundation
import Hummingbird

struct Item: Decodable {
    let name: String
    let price: Double
}

struct ItemResponse: ResponseEncodable {
    let id: Int
    let name: String
    let price: Double
}

@main
struct SimpleAPI {
    static func main() async throws {
        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"

        let router = Router()

        router.get("/") { _, _ in
            print("Received request: GET /")
            return ["message": "hello-world"]
        }

        router.get("/health") { _, _ in
            return ["status": "ok"]
        }

        router.post("/items") { request, context -> ItemResponse in
            print("Received request: POST /items")
            let item = try await request.decode(as: Item.self, context: context)
            return ItemResponse(id: 1, name: item.name, price: item.price)
        }

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: 6001))
        )

        print("Server running on http://\(hostname):6001")
        try await app.runService()
    }
}
