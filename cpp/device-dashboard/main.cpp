#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <json/json.h>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <sqlite3.h>

#include <sys/utsname.h>
#include <sys/statvfs.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static std::string exec_command(const std::string &cmd)
{
    std::string result;
    std::unique_ptr<FILE, decltype(&pclose)> pipe(popen(cmd.c_str(), "r"), pclose);
    if (!pipe) return result;
    std::array<char, 4096> buf;
    while (fgets(buf.data(), buf.size(), pipe.get()))
        result += buf.data();
    return result;
}

static std::string read_file(const std::string &path)
{
    std::ifstream f(path);
    if (!f) return {};
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

static std::string trim(const std::string &s)
{
    auto start = s.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return {};
    auto end = s.find_last_not_of(" \t\r\n");
    return s.substr(start, end - start + 1);
}

static std::vector<std::string> split(const std::string &s, char delim)
{
    std::vector<std::string> parts;
    std::istringstream ss(s);
    std::string tok;
    while (std::getline(ss, tok, delim))
        parts.push_back(tok);
    return parts;
}

static std::string json_to_string(const Json::Value &v)
{
    Json::StreamWriterBuilder wb;
    wb["indentation"] = "";
    return Json::writeString(wb, v);
}

// ---------------------------------------------------------------------------
// SQLite database
// ---------------------------------------------------------------------------

static std::mutex dbMutex;
static const char *DB_PATH = "/data/cars.db";

static sqlite3 *open_db()
{
    // Ensure /data directory exists
    std::string dir = "/data";
    mkdir(dir.c_str(), 0755);

    sqlite3 *db = nullptr;
    if (sqlite3_open(DB_PATH, &db) != SQLITE_OK)
    {
        std::cerr << "Failed to open database: " << sqlite3_errmsg(db) << std::endl;
        return nullptr;
    }
    const char *sql =
        "CREATE TABLE IF NOT EXISTS cars ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  make TEXT NOT NULL,"
        "  model TEXT NOT NULL,"
        "  color TEXT NOT NULL,"
        "  year INTEGER NOT NULL,"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  updated_at TEXT"
        ")";
    char *err = nullptr;
    sqlite3_exec(db, sql, nullptr, nullptr, &err);
    if (err)
    {
        std::cerr << "SQL error: " << err << std::endl;
        sqlite3_free(err);
    }
    return db;
}

static Json::Value row_to_json(sqlite3_stmt *stmt)
{
    Json::Value obj;
    int cols = sqlite3_column_count(stmt);
    for (int i = 0; i < cols; i++)
    {
        const char *name = sqlite3_column_name(stmt, i);
        int type = sqlite3_column_type(stmt, i);
        if (type == SQLITE_NULL)
            obj[name] = Json::nullValue;
        else if (type == SQLITE_INTEGER)
            obj[name] = (Json::Int64)sqlite3_column_int64(stmt, i);
        else if (type == SQLITE_FLOAT)
            obj[name] = sqlite3_column_double(stmt, i);
        else
        {
            const char *text = (const char *)sqlite3_column_text(stmt, i);
            obj[name] = text ? text : "";
        }
    }
    return obj;
}

// ---------------------------------------------------------------------------
// Device listing helpers
// ---------------------------------------------------------------------------

static Json::Value list_cameras()
{
    Json::Value arr(Json::arrayValue);
    // Find /dev/video* devices
    for (int i = 0; i < 64; i++)
    {
        std::string path = "/dev/video" + std::to_string(i);
        std::ifstream test(path);
        if (!test.good()) continue;
        test.close();

        // Check if it's a capture device
        std::string info = exec_command("v4l2-ctl --device " + path + " --all 2>/dev/null");
        if (info.find("Video Capture") == std::string::npos) continue;

        std::string name = path;
        std::string card_info = exec_command("v4l2-ctl --device " + path + " --info 2>/dev/null");
        for (auto &line : split(card_info, '\n'))
        {
            if (line.find("Card type") != std::string::npos)
            {
                auto pos = line.find(':');
                if (pos != std::string::npos)
                    name = trim(line.substr(pos + 1));
                break;
            }
        }

        Json::Value cam;
        cam["id"] = path;
        cam["name"] = name;
        arr.append(cam);
    }
    return arr;
}

static Json::Value list_alsa_devices(const std::string &cmd)
{
    Json::Value arr(Json::arrayValue);
    std::string out = exec_command(cmd + " 2>/dev/null");
    for (auto &line : split(out, '\n'))
    {
        if (line.substr(0, 5) != "card ") continue;
        auto parts = split(line, ':');
        if (parts.size() < 2) continue;
        // Extract card number
        auto tokens = split(line, ' ');
        if (tokens.size() < 2) continue;
        std::string card = tokens[1];
        // Remove trailing colon
        if (!card.empty() && card.back() == ':')
            card.pop_back();
        // Extract name
        std::string name = parts[1];
        auto bracket = name.find('[');
        if (bracket != std::string::npos)
            name = name.substr(0, bracket);
        name = trim(name);

        Json::Value dev;
        dev["id"] = "hw:" + card + ",0";
        dev["name"] = name;
        arr.append(dev);
    }
    return arr;
}

// ---------------------------------------------------------------------------
// GStreamer capture singleton
// ---------------------------------------------------------------------------

class GstCaptureSink
{
public:
    using ClientId = const drogon::WebSocketConnectionPtr;

    explicit GstCaptureSink(size_t maxQueue) : maxQueue_(maxQueue) {}
    virtual ~GstCaptureSink()
    {
        stop();
    }

    // Try to start the pipeline; returns false if no device is available.
    bool start()
    {
        for (auto &desc : buildPipelines())
        {
            GError *err = nullptr;
            GstElement *p = gst_parse_launch(desc.c_str(), &err);
            if (err)
            {
                g_error_free(err);
                if (p) gst_object_unref(p);
                continue;
            }
            GstStateChangeReturn ret = gst_element_set_state(p, GST_STATE_PAUSED);
            if (ret == GST_STATE_CHANGE_FAILURE)
            {
                gst_element_set_state(p, GST_STATE_NULL);
                gst_object_unref(p);
                continue;
            }
            if (ret == GST_STATE_CHANGE_ASYNC)
            {
                GstState state;
                ret = gst_element_get_state(p, &state, nullptr, 5 * GST_SECOND);
                if (ret == GST_STATE_CHANGE_FAILURE)
                {
                    gst_element_set_state(p, GST_STATE_NULL);
                    gst_object_unref(p);
                    continue;
                }
            }
            // Connect appsink callback
            GstElement *sink = gst_bin_get_by_name(GST_BIN(p), "sink");
            if (sink)
            {
                g_object_set(sink, "emit-signals", TRUE, nullptr);
                g_signal_connect(sink, "new-sample",
                                 G_CALLBACK(on_new_sample_static), this);
                gst_object_unref(sink);
            }
            gst_element_set_state(p, GST_STATE_PLAYING);
            pipeline_ = p;
            std::cout << "GStreamer pipeline ready: " << desc << std::endl;
            return true;
        }
        return false;
    }

    void stop()
    {
        if (pipeline_)
        {
            gst_element_set_state(pipeline_, GST_STATE_NULL);
            gst_object_unref(pipeline_);
            pipeline_ = nullptr;
        }
    }

    // Called from Drogon WS thread when a client connects. Returns true on
    // success.
    bool addClient(const drogon::WebSocketConnectionPtr &conn)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!pipeline_)
        {
            if (!start())
                return false;
        }
        clients_.insert(conn);
        return true;
    }

    void removeClient(const drogon::WebSocketConnectionPtr &conn)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        clients_.erase(conn);
        if (clients_.empty())
            stop();
    }

    void switchDevice(const std::string &device)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop();
        currentDevice_ = device;
        if (!clients_.empty())
            start();
        std::cout << "Switched device to: " << device << std::endl;
    }

    // Retrieve the latest buffer for broadcast. Returns empty vector if none.
    std::vector<char> popLatest()
    {
        std::lock_guard<std::mutex> lock(bufMutex_);
        if (buffers_.empty()) return {};
        auto buf = std::move(buffers_.front());
        buffers_.pop_front();
        return buf;
    }

    bool hasClients()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return !clients_.empty();
    }

    std::unordered_set<drogon::WebSocketConnectionPtr> getClients()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return clients_;
    }

protected:
    virtual std::vector<std::string> buildPipelines() = 0;
    std::string currentDevice_;

private:
    static GstFlowReturn on_new_sample_static(GstElement *sink, gpointer data)
    {
        auto *self = static_cast<GstCaptureSink *>(data);
        return self->onNewSample(sink);
    }

    GstFlowReturn onNewSample(GstElement *sink)
    {
        GstSample *sample = nullptr;
        g_signal_emit_by_name(sink, "pull-sample", &sample);
        if (!sample) return GST_FLOW_OK;

        GstBuffer *buf = gst_sample_get_buffer(sample);
        if (!buf)
        {
            gst_sample_unref(sample);
            return GST_FLOW_OK;
        }

        GstMapInfo map;
        if (gst_buffer_map(buf, &map, GST_MAP_READ))
        {
            std::vector<char> data(map.data, map.data + map.size);
            gst_buffer_unmap(buf, &map);

            std::lock_guard<std::mutex> lock(bufMutex_);
            buffers_.push_back(std::move(data));
            while (buffers_.size() > maxQueue_)
                buffers_.pop_front();
        }
        gst_sample_unref(sample);
        return GST_FLOW_OK;
    }

    GstElement *pipeline_ = nullptr;
    std::mutex mutex_;
    std::mutex bufMutex_;
    std::deque<std::vector<char>> buffers_;
    size_t maxQueue_;
    std::unordered_set<drogon::WebSocketConnectionPtr> clients_;
};

// -- MJPEG Camera Singleton --

class MJPEGCamera : public GstCaptureSink
{
public:
    MJPEGCamera() : GstCaptureSink(2) {}

protected:
    std::vector<std::string> buildPipelines() override
    {
        std::string appsink =
            "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false";
        std::string src = currentDevice_.empty()
                              ? "v4l2src"
                              : "v4l2src device=" + currentDevice_;
        return {
            src + " ! image/jpeg ! " + appsink,
            src + " ! image/jpeg,width=640,height=480 ! " + appsink,
            src + " ! videoconvert ! jpegenc quality=70 ! " + appsink,
        };
    }
};

// -- Audio PCM Singleton --

class AudioCapture : public GstCaptureSink
{
public:
    AudioCapture() : GstCaptureSink(4) {}

protected:
    std::vector<std::string> buildPipelines() override
    {
        std::string appsink =
            "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";
        std::string pcm = "audio/x-raw,format=S16LE,channels=1,rate=16000";

        std::vector<std::string> pipelines;
        if (!currentDevice_.empty())
        {
            pipelines.push_back(
                "alsasrc device=\"" + currentDevice_ +
                "\" ! audioconvert ! audioresample ! " + pcm + " ! " + appsink);
        }
        else
        {
            // Try each detected microphone
            Json::Value mics = list_alsa_devices("arecord -l");
            for (const auto &mic : mics)
            {
                pipelines.push_back(
                    "alsasrc device=\"" + mic["id"].asString() +
                    "\" ! audioconvert ! audioresample ! " + pcm + " ! " + appsink);
            }
            // Fallback to default device
            pipelines.push_back(
                "alsasrc ! audioconvert ! audioresample ! " + pcm + " ! " + appsink);
        }
        return pipelines;
    }
};

// Global singletons
static MJPEGCamera gCamera;
static AudioCapture gAudio;

// ---------------------------------------------------------------------------
// WebSocket controllers
// ---------------------------------------------------------------------------

class CameraStreamWS : public drogon::WebSocketController<CameraStreamWS>
{
public:
    void handleNewMessage(const drogon::WebSocketConnectionPtr &conn,
                          std::string &&message,
                          const drogon::WebSocketMessageType &type) override
    {
        if (type == drogon::WebSocketMessageType::Text)
        {
            Json::CharReaderBuilder rb;
            Json::Value msg;
            std::istringstream ss(message);
            if (Json::parseFromStream(rb, ss, &msg, nullptr))
            {
                if (msg.isMember("switch_camera"))
                    gCamera.switchDevice(msg["switch_camera"].asString());
            }
        }
    }

    void handleNewConnection(const drogon::HttpRequestPtr &req,
                             const drogon::WebSocketConnectionPtr &conn) override
    {
        if (!gCamera.addClient(conn))
        {
            conn->shutdown(drogon::CloseCode::kViolation, "No camera available");
        }
    }

    void handleConnectionClosed(const drogon::WebSocketConnectionPtr &conn) override
    {
        gCamera.removeClient(conn);
    }

    WS_PATH_LIST_BEGIN
    WS_PATH_ADD("/api/camera/stream");
    WS_PATH_LIST_END
};

class AudioStreamWS : public drogon::WebSocketController<AudioStreamWS>
{
public:
    void handleNewMessage(const drogon::WebSocketConnectionPtr &conn,
                          std::string &&message,
                          const drogon::WebSocketMessageType &type) override
    {
        if (type == drogon::WebSocketMessageType::Text)
        {
            Json::CharReaderBuilder rb;
            Json::Value msg;
            std::istringstream ss(message);
            if (Json::parseFromStream(rb, ss, &msg, nullptr))
            {
                if (msg.isMember("switch_microphone"))
                    gAudio.switchDevice(msg["switch_microphone"].asString());
            }
        }
    }

    void handleNewConnection(const drogon::HttpRequestPtr &req,
                             const drogon::WebSocketConnectionPtr &conn) override
    {
        if (!gAudio.addClient(conn))
        {
            conn->shutdown(drogon::CloseCode::kViolation, "No microphone available");
        }
    }

    void handleConnectionClosed(const drogon::WebSocketConnectionPtr &conn) override
    {
        gAudio.removeClient(conn);
    }

    WS_PATH_LIST_BEGIN
    WS_PATH_ADD("/api/audio/stream");
    WS_PATH_LIST_END
};

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main()
{
    // Initialize GStreamer
    gst_init(nullptr, nullptr);

    // Run GLib main loop in background thread
    GMainLoop *gloop = g_main_loop_new(nullptr, FALSE);
    std::thread glibThread([gloop]() { g_main_loop_run(gloop); });
    glibThread.detach();

    const char *hostname = std::getenv("WENDY_HOSTNAME");
    if (hostname)
        std::cout << "WENDY_HOSTNAME: " << hostname << std::endl;

    // ------------------------------------------------------------------
    // REST: /api/cars
    // ------------------------------------------------------------------

    // GET /api/cars
    drogon::app().registerHandler(
        "/api/cars",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            std::lock_guard<std::mutex> lock(dbMutex);
            sqlite3 *db = open_db();
            if (!db)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Database error"));
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }
            sqlite3_stmt *stmt = nullptr;
            sqlite3_prepare_v2(db, "SELECT * FROM cars ORDER BY id", -1, &stmt, nullptr);
            Json::Value arr(Json::arrayValue);
            while (sqlite3_step(stmt) == SQLITE_ROW)
                arr.append(row_to_json(stmt));
            sqlite3_finalize(stmt);
            sqlite3_close(db);

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

            std::lock_guard<std::mutex> lock(dbMutex);
            sqlite3 *db = open_db();
            if (!db)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Database error"));
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }

            sqlite3_stmt *stmt = nullptr;
            sqlite3_prepare_v2(db,
                               "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)",
                               -1, &stmt, nullptr);
            sqlite3_bind_text(stmt, 1, (*body)["make"].asCString(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 2, (*body)["model"].asCString(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 3, (*body)["color"].asCString(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_int(stmt, 4, (*body)["year"].asInt());
            sqlite3_step(stmt);
            sqlite3_finalize(stmt);

            sqlite3_int64 lastId = sqlite3_last_insert_rowid(db);
            sqlite3_prepare_v2(db, "SELECT * FROM cars WHERE id = ?", -1, &stmt, nullptr);
            sqlite3_bind_int64(stmt, 1, lastId);

            Json::Value car;
            if (sqlite3_step(stmt) == SQLITE_ROW)
                car = row_to_json(stmt);
            sqlite3_finalize(stmt);
            sqlite3_close(db);

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
            std::lock_guard<std::mutex> lock(dbMutex);
            sqlite3 *db = open_db();
            if (!db)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Database error"));
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }

            sqlite3_stmt *stmt = nullptr;
            sqlite3_prepare_v2(db, "SELECT * FROM cars WHERE id = ?", -1, &stmt, nullptr);
            sqlite3_bind_int(stmt, 1, id);

            if (sqlite3_step(stmt) == SQLITE_ROW)
            {
                Json::Value car = row_to_json(stmt);
                sqlite3_finalize(stmt);
                sqlite3_close(db);
                auto resp = drogon::HttpResponse::newHttpJsonResponse(car);
                callback(resp);
            }
            else
            {
                sqlite3_finalize(stmt);
                sqlite3_close(db);
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Car not found"));
                resp->setStatusCode(drogon::k404NotFound);
                callback(resp);
            }
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

            std::lock_guard<std::mutex> lock(dbMutex);
            sqlite3 *db = open_db();
            if (!db)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Database error"));
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }

            sqlite3_stmt *stmt = nullptr;
            sqlite3_prepare_v2(
                db,
                "UPDATE cars SET make=?, model=?, color=?, year=?, updated_at=datetime('now') WHERE id=?",
                -1, &stmt, nullptr);
            sqlite3_bind_text(stmt, 1, (*body)["make"].asCString(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 2, (*body)["model"].asCString(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(stmt, 3, (*body)["color"].asCString(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_int(stmt, 4, (*body)["year"].asInt());
            sqlite3_bind_int(stmt, 5, id);
            sqlite3_step(stmt);
            sqlite3_finalize(stmt);

            sqlite3_prepare_v2(db, "SELECT * FROM cars WHERE id = ?", -1, &stmt, nullptr);
            sqlite3_bind_int(stmt, 1, id);

            if (sqlite3_step(stmt) == SQLITE_ROW)
            {
                Json::Value car = row_to_json(stmt);
                sqlite3_finalize(stmt);
                sqlite3_close(db);
                auto resp = drogon::HttpResponse::newHttpJsonResponse(car);
                callback(resp);
            }
            else
            {
                sqlite3_finalize(stmt);
                sqlite3_close(db);
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Car not found"));
                resp->setStatusCode(drogon::k404NotFound);
                callback(resp);
            }
        },
        {drogon::Put});

    // DELETE /api/cars/{id}
    drogon::app().registerHandler(
        "/api/cars/{id}",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback,
           int id) {
            std::lock_guard<std::mutex> lock(dbMutex);
            sqlite3 *db = open_db();
            if (!db)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Database error"));
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }

            sqlite3_stmt *stmt = nullptr;
            sqlite3_prepare_v2(db, "DELETE FROM cars WHERE id = ?", -1, &stmt, nullptr);
            sqlite3_bind_int(stmt, 1, id);
            sqlite3_step(stmt);
            sqlite3_finalize(stmt);
            int changes = sqlite3_changes(db);
            sqlite3_close(db);

            if (changes == 0)
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    Json::Value("Car not found"));
                resp->setStatusCode(drogon::k404NotFound);
                callback(resp);
            }
            else
            {
                auto resp = drogon::HttpResponse::newHttpResponse();
                resp->setStatusCode(drogon::k204NoContent);
                callback(resp);
            }
        },
        {drogon::Delete});

    // ------------------------------------------------------------------
    // REST: device listing
    // ------------------------------------------------------------------

    // GET /api/cameras
    drogon::app().registerHandler(
        "/api/cameras",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            auto resp = drogon::HttpResponse::newHttpJsonResponse(list_cameras());
            callback(resp);
        },
        {drogon::Get});

    // GET /api/microphones
    drogon::app().registerHandler(
        "/api/microphones",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            auto resp = drogon::HttpResponse::newHttpJsonResponse(
                list_alsa_devices("arecord -l"));
            callback(resp);
        },
        {drogon::Get});

    // GET /api/speakers
    drogon::app().registerHandler(
        "/api/speakers",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            auto resp = drogon::HttpResponse::newHttpJsonResponse(
                list_alsa_devices("aplay -l"));
            callback(resp);
        },
        {drogon::Get});

    // ------------------------------------------------------------------
    // REST: /api/gpu
    // ------------------------------------------------------------------

    drogon::app().registerHandler(
        "/api/gpu",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            Json::Value info;
            info["available"] = false;

            std::string out = exec_command(
                "nvidia-smi --query-gpu=name,memory.total,driver_version,temperature.gpu "
                "--format=csv,noheader,nounits 2>/dev/null");
            out = trim(out);

            if (!out.empty())
            {
                auto parts = split(out, ',');
                info["available"] = true;
                if (parts.size() > 0) info["name"] = trim(parts[0]);
                if (parts.size() > 1) info["memory"] = trim(parts[1]) + " MiB";
                if (parts.size() > 2) info["driver"] = trim(parts[2]);
                if (parts.size() > 3) info["temperature"] = trim(parts[3]) + "\xC2\xB0" "C";
            }
            else
            {
                // Fallback: read thermal zone
                std::string temp = trim(read_file("/sys/class/thermal/thermal_zone0/temp"));
                if (!temp.empty())
                {
                    double t = std::stod(temp) / 1000.0;
                    char buf[32];
                    snprintf(buf, sizeof(buf), "%.1f\xC2\xB0" "C", t);
                    info["available"] = true;
                    info["name"] = "ARM GPU";
                    info["temperature"] = std::string(buf);
                }
            }

            auto resp = drogon::HttpResponse::newHttpJsonResponse(info);
            callback(resp);
        },
        {drogon::Get});

    // ------------------------------------------------------------------
    // REST: /api/system
    // ------------------------------------------------------------------

    drogon::app().registerHandler(
        "/api/system",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            Json::Value info;

            // Hostname
            const char *wh = std::getenv("WENDY_HOSTNAME");
            if (wh)
                info["hostname"] = wh;
            else
            {
                char hbuf[256] = {};
                gethostname(hbuf, sizeof(hbuf));
                info["hostname"] = hbuf;
            }

            info["platform"] = "Linux";

            // Architecture
            struct utsname uts;
            if (uname(&uts) == 0)
                info["architecture"] = uts.machine;

            // Memory
            Json::Value mem;
            {
                std::string mi = read_file("/proc/meminfo");
                long total_kb = 0, avail_kb = 0;
                for (auto &line : split(mi, '\n'))
                {
                    if (line.find("MemTotal") == 0)
                    {
                        auto toks = split(line, ' ');
                        for (auto &t : toks)
                        {
                            try { total_kb = std::stol(t); } catch (...) {}
                        }
                    }
                    else if (line.find("MemAvailable") == 0)
                    {
                        auto toks = split(line, ' ');
                        for (auto &t : toks)
                        {
                            try { avail_kb = std::stol(t); } catch (...) {}
                        }
                    }
                }
                if (total_kb > 0)
                {
                    mem["total"] = std::to_string(total_kb / 1024) + " MB";
                    mem["free"] = std::to_string(avail_kb / 1024) + " MB";
                    mem["used"] = std::to_string((total_kb - avail_kb) / 1024) + " MB";
                }
            }
            info["memory"] = mem;

            // Disk
            Json::Value disk;
            {
                struct statvfs st;
                if (statvfs("/", &st) == 0)
                {
                    unsigned long long total = (unsigned long long)st.f_blocks * st.f_frsize;
                    unsigned long long free_b = (unsigned long long)st.f_bfree * st.f_frsize;
                    unsigned long long used = total - free_b;
                    disk["total"] = std::to_string(total / (1024ULL * 1024 * 1024)) + " GB";
                    disk["used"] = std::to_string(used / (1024ULL * 1024 * 1024)) + " GB";
                    disk["free"] = std::to_string(free_b / (1024ULL * 1024 * 1024)) + " GB";
                }
            }
            info["disk"] = disk;

            // CPU
            Json::Value cpu;
            {
                std::string ci = read_file("/proc/cpuinfo");
                std::string model_name;
                for (auto &line : split(ci, '\n'))
                {
                    if (line.find("model name") == 0)
                    {
                        auto pos = line.find(':');
                        if (pos != std::string::npos)
                        {
                            model_name = trim(line.substr(pos + 1));
                            break;
                        }
                    }
                }
                cpu["model"] = model_name.empty() ? "unknown" : model_name;
                long cores = sysconf(_SC_NPROCESSORS_ONLN);
                cpu["cores"] = (int)(cores > 0 ? cores : 0);
            }
            info["cpu"] = cpu;

            // Uptime
            {
                std::string ut = read_file("/proc/uptime");
                if (!ut.empty())
                {
                    double secs = std::stod(split(ut, ' ')[0]);
                    int h = (int)(secs / 3600);
                    int m = (int)(((int)secs % 3600) / 60);
                    info["uptime"] = std::to_string(h) + "h " + std::to_string(m) + "m";
                }
                else
                    info["uptime"] = "";
            }

            auto resp = drogon::HttpResponse::newHttpJsonResponse(info);
            callback(resp);
        },
        {drogon::Get});

    // ------------------------------------------------------------------
    // Broadcast timers for WebSocket streams
    // ------------------------------------------------------------------

    drogon::app().getLoop()->runAfter(1.0, []() {
        // Camera broadcast: every 33ms (~30 fps)
        drogon::app().getLoop()->runEvery(0.033, []() {
            if (!gCamera.hasClients()) return;
            auto data = gCamera.popLatest();
            if (data.empty()) return;
            auto clients = gCamera.getClients();
            for (auto &conn : clients)
            {
                if (conn->connected())
                    conn->send(data.data(), data.size(),
                               drogon::WebSocketMessageType::Binary);
            }
        });

        // Audio broadcast: every 20ms (50 Hz)
        drogon::app().getLoop()->runEvery(0.020, []() {
            if (!gAudio.hasClients()) return;
            auto data = gAudio.popLatest();
            if (data.empty()) return;
            auto clients = gAudio.getClients();
            for (auto &conn : clients)
            {
                if (conn->connected())
                    conn->send(data.data(), data.size(),
                               drogon::WebSocketMessageType::Binary);
            }
        });
    });

    // ------------------------------------------------------------------
    // Start server
    // ------------------------------------------------------------------

    std::cout << "Starting server on 0.0.0.0:{{.PORT}}" << std::endl;

    drogon::app()
        .setDocumentRoot("./static")
        .addListener("0.0.0.0", {{.PORT}})
        .run();

    g_main_loop_quit(gloop);
    g_main_loop_unref(gloop);
    return 0;
}
