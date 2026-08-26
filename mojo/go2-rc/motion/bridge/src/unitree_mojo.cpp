#include "unitree_mojo.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <unitree/idl/go2/LowState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/go2/sport/sport_client.hpp>

namespace {
constexpr float kMaxVx = 0.6F;
constexpr float kMaxVy = 0.4F;
constexpr float kMaxVyaw = 1.0F;
constexpr uint32_t kDefaultWatchdogMs = 1000;
constexpr uint32_t kMinMoveMs = 100;
constexpr uint32_t kMaxMoveMs = 10000;
constexpr uint32_t kMoveWatchdogSlopMs = 500;
thread_local std::string g_last_create_error;

float clamp(float value, float limit) {
  return std::max(-limit, std::min(limit, value));
}

int32_t copy_string(const std::string &value, char *buffer, size_t size) {
  if (buffer == nullptr || size == 0)
    return -2;
  const size_t count = std::min(value.size(), size - 1);
  std::memcpy(buffer, value.data(), count);
  buffer[count] = '\0';
  return static_cast<int32_t>(count);
}
} // namespace

struct unitree_mojo_client {
  using LowState = unitree_go::msg::dds_::LowState_;
  using Subscriber = unitree::robot::ChannelSubscriber<LowState>;

  explicit unitree_mojo_client(const std::string &network_interface,
                               uint32_t watchdog_ms)
      : velocity_watchdog_ms(watchdog_ms == 0 ? kDefaultWatchdogMs
                                              : watchdog_ms) {
    (void)network_interface;
    sport.SetTimeout(0.5F);
    sport.Init();
    subscriber = std::make_unique<Subscriber>("rt/lowstate");
    subscriber->InitChannel(std::bind(&unitree_mojo_client::on_low_state, this,
                                      std::placeholders::_1),
                            10);
    watchdog_thread = std::thread(&unitree_mojo_client::watchdog_loop, this);
    ready = true;
  }

  ~unitree_mojo_client() {
    {
      std::lock_guard<std::mutex> lock(watchdog_mutex);
      shutting_down = true;
      watchdog_armed = false;
    }
    watchdog_cv.notify_all();
    if (watchdog_thread.joinable())
      watchdog_thread.join();
    if (ready) {
      std::lock_guard<std::mutex> lock(command_mutex);
      sport.SetTimeout(0.5F);
      (void)sport.StopMove();
    }
    if (subscriber)
      subscriber->CloseChannel();
    ready = false;
  }

  void on_low_state(const void *message) {
    if (message == nullptr)
      return;
    const auto &input = *static_cast<const LowState *>(message);
    unitree_mojo_state next{};
    next.abi_version = UNITREE_MOJO_ABI_VERSION;
    next.valid = 1;
    next.battery_soc = input.bms_state().soc();
    next.power_v = input.power_v();
    for (size_t i = 0; i < 3; ++i)
      next.imu_rpy[i] = input.imu_state().rpy()[i];
    for (size_t i = 0; i < 4; ++i)
      next.foot_force[i] = input.foot_force()[i];
    next.tick = input.tick();
    std::lock_guard<std::mutex> lock(state_mutex);
    state = next;
  }

  void set_error(const std::string &message) {
    std::lock_guard<std::mutex> lock(error_mutex);
    last_error = message;
  }

  int32_t command(const char *name, float timeout,
                  const std::function<int32_t()> &call) {
    if (!ready) {
      set_error("client is not ready");
      return -3;
    }
    try {
      std::lock_guard<std::mutex> lock(command_mutex);
      sport.SetTimeout(timeout);
      const int32_t result = call();
      if (result != 0) {
        set_error(std::string(name) + " failed with Unitree status " +
                  std::to_string(result));
      }
      return result;
    } catch (const std::exception &error) {
      set_error(std::string(name) + " threw: " + error.what());
      return -4;
    } catch (...) {
      set_error(std::string(name) + " threw an unknown exception");
      return -4;
    }
  }

  void arm_watchdog(uint32_t delay_ms) {
    {
      std::lock_guard<std::mutex> lock(watchdog_mutex);
      watchdog_deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(delay_ms);
      watchdog_armed = true;
      ++watchdog_generation;
    }
    watchdog_cv.notify_all();
  }

  void disarm_watchdog() {
    {
      std::lock_guard<std::mutex> lock(watchdog_mutex);
      watchdog_armed = false;
      ++watchdog_generation;
    }
    watchdog_cv.notify_all();
  }

  void watchdog_loop() {
    std::unique_lock<std::mutex> lock(watchdog_mutex);
    while (!shutting_down) {
      if (!watchdog_armed) {
        watchdog_cv.wait(lock,
                         [this] { return shutting_down || watchdog_armed; });
        continue;
      }
      const auto generation = watchdog_generation;
      const auto deadline = watchdog_deadline;
      if (watchdog_cv.wait_until(lock, deadline, [this, generation] {
            return shutting_down || !watchdog_armed ||
                   watchdog_generation != generation;
          })) {
        continue;
      }
      watchdog_armed = false;
      lock.unlock();
      (void)command("watchdog StopMove", 0.5F,
                    [this] { return sport.StopMove(); });
      lock.lock();
    }
  }

  unitree::robot::go2::SportClient sport;
  std::unique_ptr<Subscriber> subscriber;
  bool ready{false};
  uint32_t velocity_watchdog_ms;
  mutable std::mutex state_mutex;
  unitree_mojo_state state{UNITREE_MOJO_ABI_VERSION,
                           0,
                           0,
                           {0, 0},
                           0.0F,
                           {0.0F, 0.0F, 0.0F},
                           {0, 0, 0, 0},
                           0};
  std::mutex command_mutex;
  mutable std::mutex error_mutex;
  std::string last_error;
  std::mutex watchdog_mutex;
  std::condition_variable watchdog_cv;
  std::thread watchdog_thread;
  bool shutting_down{false};
  bool watchdog_armed{false};
  uint64_t watchdog_generation{0};
  std::chrono::steady_clock::time_point watchdog_deadline;
};

extern "C" {
uint32_t unitree_mojo_abi_version(void) { return UNITREE_MOJO_ABI_VERSION; }

unitree_mojo_client *unitree_mojo_create(const char *network_interface,
                                         uint32_t velocity_watchdog_ms) {
  try {
    if (network_interface == nullptr || network_interface[0] == '\0') {
      g_last_create_error = "network interface must not be empty";
      return nullptr;
    }
    // SportClient creates DDS channels in its C++ constructor, so the
    // process-wide factory must be initialized before constructing our owner.
    unitree::robot::ChannelFactory::Instance()->Init(0, network_interface);
    return new unitree_mojo_client(network_interface, velocity_watchdog_ms);
  } catch (const std::exception &error) {
    g_last_create_error = error.what();
  } catch (...) {
    g_last_create_error = "unknown exception while creating Unitree client";
  }
  return nullptr;
}

void unitree_mojo_destroy(unitree_mojo_client *client) { delete client; }
int32_t unitree_mojo_is_ready(const unitree_mojo_client *client) {
  return client != nullptr && client->ready ? 1 : 0;
}

int32_t unitree_mojo_set_velocity(unitree_mojo_client *client, float vx,
                                  float vy, float vyaw) {
  if (client == nullptr)
    return -1;
  vx = clamp(vx, kMaxVx);
  vy = clamp(vy, kMaxVy);
  vyaw = clamp(vyaw, kMaxVyaw);
  const int32_t result = client->command("Move", 0.5F, [client, vx, vy, vyaw] {
    return client->sport.Move(vx, vy, vyaw);
  });
  if (result == 0)
    client->arm_watchdog(client->velocity_watchdog_ms);
  return result;
}

int32_t unitree_mojo_move_for(unitree_mojo_client *client, float vx, float vy,
                              float vyaw, uint32_t duration_ms) {
  if (client == nullptr)
    return -1;
  vx = clamp(vx, kMaxVx);
  vy = clamp(vy, kMaxVy);
  vyaw = clamp(vyaw, kMaxVyaw);
  duration_ms = std::max(kMinMoveMs, std::min(kMaxMoveMs, duration_ms));
  int32_t result = client->command("Move", 0.5F, [client, vx, vy, vyaw] {
    return client->sport.Move(vx, vy, vyaw);
  });
  if (result != 0)
    return result;
  client->arm_watchdog(duration_ms + kMoveWatchdogSlopMs);
  std::this_thread::sleep_for(std::chrono::milliseconds(duration_ms));
  result = client->command("StopMove", 0.5F,
                           [client] { return client->sport.StopMove(); });
  client->disarm_watchdog();
  return result;
}

int32_t unitree_mojo_stop(unitree_mojo_client *client) {
  if (client == nullptr)
    return -1;
  const int32_t result = client->command(
      "StopMove", 0.5F, [client] { return client->sport.StopMove(); });
  client->disarm_watchdog();
  return result;
}

#define UNITREE_MOJO_SKILL(function_name, sdk_name)                            \
  int32_t function_name(unitree_mojo_client *client) {                         \
    if (client == nullptr)                                                     \
      return -1;                                                               \
    client->disarm_watchdog();                                                 \
    return client->command(#sdk_name, 5.0F,                                    \
                           [client] { return client->sport.sdk_name(); });     \
  }
UNITREE_MOJO_SKILL(unitree_mojo_stand_up, StandUp)
UNITREE_MOJO_SKILL(unitree_mojo_sit, Sit)
UNITREE_MOJO_SKILL(unitree_mojo_stand_down, StandDown)
UNITREE_MOJO_SKILL(unitree_mojo_hello, Hello)
UNITREE_MOJO_SKILL(unitree_mojo_dance, Dance1)
#undef UNITREE_MOJO_SKILL

int32_t unitree_mojo_get_state(const unitree_mojo_client *client,
                               unitree_mojo_state *state) {
  if (client == nullptr || state == nullptr)
    return -1;
  std::lock_guard<std::mutex> lock(client->state_mutex);
  *state = client->state;
  return state->valid ? 0 : 1;
}

int32_t unitree_mojo_state_json(const unitree_mojo_client *client, char *buffer,
                                size_t buffer_size) {
  if (client == nullptr)
    return copy_string("{}", buffer, buffer_size);
  unitree_mojo_state value{};
  (void)unitree_mojo_get_state(client, &value);
  if (!value.valid)
    return copy_string("{}", buffer, buffer_size);
  char json[512];
  const int count = std::snprintf(
      json, sizeof(json),
      "{\"battery_soc\":%u,\"power_v\":%.3f,\"imu_rpy\":[%.6f,%.6f,%.6f],"
      "\"foot_force\":[%d,%d,%d,%d],\"tick\":%u}",
      static_cast<unsigned>(value.battery_soc), value.power_v, value.imu_rpy[0],
      value.imu_rpy[1], value.imu_rpy[2], static_cast<int>(value.foot_force[0]),
      static_cast<int>(value.foot_force[1]),
      static_cast<int>(value.foot_force[2]),
      static_cast<int>(value.foot_force[3]), value.tick);
  if (count < 0)
    return -4;
  if (static_cast<size_t>(count) >= sizeof(json))
    return -5;
  return copy_string(std::string(json, static_cast<size_t>(count)), buffer,
                     buffer_size);
}

int32_t unitree_mojo_last_error(const unitree_mojo_client *client, char *buffer,
                                size_t buffer_size) {
  if (client == nullptr)
    return copy_string(g_last_create_error, buffer, buffer_size);
  std::lock_guard<std::mutex> lock(client->error_mutex);
  return copy_string(client->last_error, buffer, buffer_size);
}
} // extern "C"
