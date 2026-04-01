#include <drogon/drogon.h>
#include <cstdlib>
#include <iostream>
#include <string>

using namespace drogon;

int main() {
    const char* env_hostname = std::getenv("WENDY_HOSTNAME");
    std::string hostname = env_hostname ? env_hostname : "0.0.0.0";

    app().registerHandler(
        "/",
        [](const HttpRequestPtr&, std::function<void(const HttpResponsePtr&)>&& callback) {
            std::cout << "Received request: GET /" << std::endl;
            Json::Value json;
            json["message"] = "hello-world";
            auto resp = HttpResponse::newHttpJsonResponse(json);
            callback(resp);
        },
        {Get});

    app().registerHandler(
        "/health",
        [](const HttpRequestPtr&, std::function<void(const HttpResponsePtr&)>&& callback) {
            Json::Value json;
            json["status"] = "ok";
            auto resp = HttpResponse::newHttpJsonResponse(json);
            callback(resp);
        },
        {Get});

    app().registerHandler(
        "/items",
        [](const HttpRequestPtr& req, std::function<void(const HttpResponsePtr&)>&& callback) {
            std::cout << "Received request: POST /items" << std::endl;
            auto body = req->getJsonObject();
            if (!body) {
                Json::Value err;
                err["error"] = "Invalid JSON";
                auto resp = HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(k400BadRequest);
                callback(resp);
                return;
            }

            Json::Value item;
            item["id"] = 1;
            item["name"] = (*body)["name"].asString();
            item["price"] = (*body)["price"].asDouble();
            auto resp = HttpResponse::newHttpJsonResponse(item);
            resp->setStatusCode(k201Created);
            callback(resp);
        },
        {Post});

    std::cout << "Server running on http://" << hostname << ":{{.PORT}}" << std::endl;

    app().addListener("0.0.0.0", {{.PORT}});
    app().run();

    return 0;
}
