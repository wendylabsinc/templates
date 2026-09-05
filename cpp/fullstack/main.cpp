#include <drogon/drogon.h>

int main() {
    drogon::app().registerHandler("/api/hello",
        [](const drogon::HttpRequestPtr&, std::function<void(const drogon::HttpResponsePtr&)>&& reply) {
            Json::Value body;
            body["message"] = "Hello from Wendy!";
            reply(drogon::HttpResponse::newHttpJsonResponse(body));
        }, {drogon::Get});
    drogon::app().registerHandler("/health",
        [](const drogon::HttpRequestPtr&, std::function<void(const drogon::HttpResponsePtr&)>&& reply) {
            Json::Value body;
            body["status"] = "ok";
            reply(drogon::HttpResponse::newHttpJsonResponse(body));
        }, {drogon::Get});
    drogon::app().setDocumentRoot("static").setHomePage("index.html")
        .addListener("0.0.0.0", {{.PORT}}).run();
}
