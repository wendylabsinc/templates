import Hummingbird
import Logging
import OTel
import ServiceLifecycle

struct Item: Decodable {
    let name: String
    let price: Double
}

struct ItemResponse: ResponseCodable {
    let id: Int
    let name: String
    let price: Double
}

@main
struct SimpleAPI {
    static func main() async throws {
        let observability = try OTel.bootstrap()

        let logger = Logger(label: "{{.APP_ID}}")

        let router = Router()
        router.middlewares.add(TracingMiddleware())
        router.middlewares.add(MetricsMiddleware())
        router.middlewares.add(LogRequestsMiddleware(.info))

        router.get("/") { _, _ in
            ["message": "hello-world"]
        }

        router.get("/health") { _, _ -> HTTPResponse.Status in
            .ok
        }

        router.post("/items") { request, context -> ItemResponse in
            let item = try await request.decode(as: Item.self, context: context)
            return ItemResponse(id: 1, name: item.name, price: item.price)
        }

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        let serviceGroup = ServiceGroup(
            services: [observability, app],
            gracefulShutdownSignals: [.sigterm, .sigint],
            logger: logger
        )

        try await serviceGroup.run()
    }
}
