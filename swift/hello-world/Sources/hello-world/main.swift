import WendyLite

func print(_ message: StaticString) {
    message.withUTF8Buffer { buf in
        let ptr = UnsafeRawPointer(buf.baseAddress!).assumingMemoryBound(to: CChar.self)
        Console.print(ptr, length: Int32(buf.count))
    }
}

@main
struct HelloWorldApp {
    static func main() {
        print("Hello, world")
    }
}
