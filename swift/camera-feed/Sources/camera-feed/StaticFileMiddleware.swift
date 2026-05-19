internal import Foundation
import Hummingbird
import NIOCore

func contentType(for path: String) -> String {
    let ext = URL(filePath: path).pathExtension.lowercased()
    switch ext {
    case "html":              return "text/html; charset=utf-8"
    case "css":               return "text/css; charset=utf-8"
    case "js", "mjs":         return "application/javascript; charset=utf-8"
    case "json":              return "application/json"
    case "png":               return "image/png"
    case "jpg", "jpeg":       return "image/jpeg"
    case "gif":               return "image/gif"
    case "svg":               return "image/svg+xml"
    case "ico":               return "image/x-icon"
    case "woff":              return "font/woff"
    case "woff2":             return "font/woff2"
    case "ttf":               return "font/ttf"
    case "wav":               return "audio/wav"
    case "mp3":               return "audio/mpeg"
    case "ogg":               return "audio/ogg"
    case "mp4":               return "video/mp4"
    case "webm":              return "video/webm"
    case "webp":              return "image/webp"
    case "txt":               return "text/plain; charset=utf-8"
    case "xml":               return "application/xml"
    case "pdf":               return "application/pdf"
    case "wasm":              return "application/wasm"
    case "map":               return "application/json"
    default:                  return "application/octet-stream"
    }
}

func spaHandler<C: RequestContext>(staticDir: String) -> @Sendable (Request, C) async throws -> Response {
    let resolvedRoot = URL(filePath: staticDir).standardized.path()
    return { request, _ in
        let reqPath = String(request.uri.path.drop(while: { $0 == "/" }))
        let fileURL = URL(filePath: staticDir).appending(path: reqPath).standardized

        guard fileURL.path().hasPrefix(resolvedRoot) else {
            return Response(status: .notFound, body: .init(byteBuffer: .init(string: "Not Found")))
        }

        if (try? fileURL.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true,
           let data = try? Data(contentsOf: fileURL)
        {
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            return Response(
                status: .ok,
                headers: [.contentType: contentType(for: fileURL.path())],
                body: .init(byteBuffer: buffer)
            )
        }

        let indexURL = URL(filePath: staticDir).appending(path: "index.html")
        guard let data = try? Data(contentsOf: indexURL) else {
            return Response(status: .notFound, body: .init(byteBuffer: .init(string: "Not Found")))
        }
        var buffer = ByteBuffer()
        buffer.writeBytes(data)
        return Response(
            status: .ok,
            headers: [.contentType: "text/html; charset=utf-8"],
            body: .init(byteBuffer: buffer)
        )
    }
}
