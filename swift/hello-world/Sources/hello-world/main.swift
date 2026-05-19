import WendyLite

func print(_ message: StaticString) {
    message.withUTF8Buffer { buf in
        let ptr = UnsafeRawPointer(buf.baseAddress!).assumingMemoryBound(to: CChar.self)
        Console.print(ptr, length: Int32(buf.count))
    }
}

@_cdecl("_start")
func start() {
    print("Hello, world")
}
