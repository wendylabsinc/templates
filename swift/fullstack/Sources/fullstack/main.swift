import Foundation
import Hummingbird

struct Car: Codable, Sendable {
    let id: Int
    let make: String
    let model: String
    let color: String
    let year: Int
}

struct CarInput: Decodable {
    let make: String
    let model: String
    let color: String
    let year: Int
}

actor CarStore {
    private var cars: [Int: Car] = [:]
    private var nextID: Int = 1

    func all() -> [Car] {
        Array(cars.values).sorted { $0.id < $1.id }
    }

    func get(id: Int) -> Car? {
        cars[id]
    }

    func create(input: CarInput) -> Car {
        let car = Car(id: nextID, make: input.make, model: input.model, color: input.color, year: input.year)
        cars[nextID] = car
        nextID += 1
        return car
    }

    func update(id: Int, input: CarInput) -> Car? {
        guard cars[id] != nil else { return nil }
        let car = Car(id: id, make: input.make, model: input.model, color: input.color, year: input.year)
        cars[id] = car
        return car
    }

    func delete(id: Int) -> Bool {
        cars.removeValue(forKey: id) != nil
    }
}

let store = CarStore()

let router = Router()

router.group("api/cars") { group in
    group.get { _, _ -> [Car] in
        await store.all()
    }

    group.post { request, context -> Car in
        let input = try await request.decode(as: CarInput.self, context: context)
        return await store.create(input: input)
    }

    group.get(":id") { _, context -> Car in
        guard let id = context.parameters.get("id", as: Int.self) else {
            throw HTTPError(.badRequest, message: "Invalid car ID")
        }
        guard let car = await store.get(id: id) else {
            throw HTTPError(.notFound, message: "Car not found")
        }
        return car
    }

    group.put(":id") { request, context -> Car in
        guard let id = context.parameters.get("id", as: Int.self) else {
            throw HTTPError(.badRequest, message: "Invalid car ID")
        }
        let input = try await request.decode(as: CarInput.self, context: context)
        guard let car = await store.update(id: id, input: input) else {
            throw HTTPError(.notFound, message: "Car not found")
        }
        return car
    }

    group.delete(":id") { _, context -> HTTPResponse.Status in
        guard let id = context.parameters.get("id", as: Int.self) else {
            throw HTTPError(.badRequest, message: "Invalid car ID")
        }
        guard await store.delete(id: id) else {
            throw HTTPError(.notFound, message: "Car not found")
        }
        return .noContent
    }
}

router.addMiddleware {
    FileMiddleware("static", searchForIndexHtml: true)
}

let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"

let app = Application(
    router: router,
    configuration: .init(
        address: .hostname("0.0.0.0", port: {{.PORT}})
    )
)

print("Starting server on 0.0.0.0:{{.PORT}}")
if !hostname.isEmpty {
    print("WENDY_HOSTNAME: \(hostname)")
}

try await app.runService()
