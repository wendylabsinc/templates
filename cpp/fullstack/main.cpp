#include <drogon/drogon.h>
#include <json/json.h>
#include <mutex>
#include <vector>
#include <cstdlib>
#include <iostream>
#include <string>

static std::mutex carsMutex;
static std::vector<Json::Value> cars;
static int nextId = 1;

int main()
{
    const char *hostname = std::getenv("WENDY_HOSTNAME");
    if (hostname)
    {
        std::cout << "WENDY_HOSTNAME: " << hostname << std::endl;
    }

    // GET /api/cars
    drogon::app().registerHandler(
        "/api/cars",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            std::lock_guard<std::mutex> lock(carsMutex);
            Json::Value arr(Json::arrayValue);
            for (const auto &car : cars)
            {
                arr.append(car);
            }
            auto resp = drogon::HttpResponse::newHttpJsonResponse(arr);
            callback(resp);
        },
        {drogon::Get});

    // POST /api/cars
    drogon::app().registerHandler(
        "/api/cars",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            auto body = req->getJsonObject();
            if (!body)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Invalid JSON"));
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            Json::Value car;
            {
                std::lock_guard<std::mutex> lock(carsMutex);
                car["id"] = nextId++;
                car["make"] = (*body)["make"];
                car["model"] = (*body)["model"];
                car["color"] = (*body)["color"];
                car["year"] = (*body)["year"];
                cars.push_back(car);
            }

            auto resp = drogon::HttpResponse::newHttpJsonResponse(car);
            resp->setStatusCode(drogon::k201Created);
            callback(resp);
        },
        {drogon::Post});

    // GET /api/cars/{id}
    drogon::app().registerHandler(
        "/api/cars/{id}",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback,
           int id) {
            std::lock_guard<std::mutex> lock(carsMutex);
            for (const auto &car : cars)
            {
                if (car["id"].asInt() == id)
                {
                    auto resp = drogon::HttpResponse::newHttpJsonResponse(car);
                    callback(resp);
                    return;
                }
            }
            auto resp = drogon::HttpResponse::newHttpJsonResponse(
                Json::Value("Not found"));
            resp->setStatusCode(drogon::k404NotFound);
            callback(resp);
        },
        {drogon::Get});

    // PUT /api/cars/{id}
    drogon::app().registerHandler(
        "/api/cars/{id}",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback,
           int id) {
            auto body = req->getJsonObject();
            if (!body)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Invalid JSON"));
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            std::lock_guard<std::mutex> lock(carsMutex);
            for (auto &car : cars)
            {
                if (car["id"].asInt() == id)
                {
                    car["make"] = (*body)["make"];
                    car["model"] = (*body)["model"];
                    car["color"] = (*body)["color"];
                    car["year"] = (*body)["year"];
                    auto resp = drogon::HttpResponse::newHttpJsonResponse(car);
                    callback(resp);
                    return;
                }
            }
            auto resp = drogon::HttpResponse::newHttpJsonResponse(
                Json::Value("Not found"));
            resp->setStatusCode(drogon::k404NotFound);
            callback(resp);
        },
        {drogon::Put});

    // DELETE /api/cars/{id}
    drogon::app().registerHandler(
        "/api/cars/{id}",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback,
           int id) {
            std::lock_guard<std::mutex> lock(carsMutex);
            for (auto it = cars.begin(); it != cars.end(); ++it)
            {
                if ((*it)["id"].asInt() == id)
                {
                    cars.erase(it);
                    auto resp = drogon::HttpResponse::newHttpResponse();
                    resp->setStatusCode(drogon::k204NoContent);
                    callback(resp);
                    return;
                }
            }
            auto resp = drogon::HttpResponse::newHttpJsonResponse(
                Json::Value("Not found"));
            resp->setStatusCode(drogon::k404NotFound);
            callback(resp);
        },
        {drogon::Delete});

    std::cout << "Starting server on 0.0.0.0:{{.PORT}}" << std::endl;

    drogon::app()
        .setDocumentRoot("./static")
        .addListener("0.0.0.0", {{.PORT}})
        .run();

    return 0;
}
