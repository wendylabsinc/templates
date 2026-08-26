#ifndef UNITREE_MOJO_H
#define UNITREE_MOJO_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define UNITREE_MOJO_EXPORT __declspec(dllexport)
#else
#define UNITREE_MOJO_EXPORT __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define UNITREE_MOJO_ABI_VERSION 1

typedef struct unitree_mojo_client unitree_mojo_client;

typedef struct unitree_mojo_state {
  uint32_t abi_version;
  uint8_t valid;
  uint8_t battery_soc;
  uint8_t reserved[2];
  float power_v;
  float imu_rpy[3];
  int16_t foot_force[4];
  uint32_t tick;
} unitree_mojo_state;

UNITREE_MOJO_EXPORT uint32_t unitree_mojo_abi_version(void);
UNITREE_MOJO_EXPORT unitree_mojo_client *
unitree_mojo_create(const char *network_interface,
                    uint32_t velocity_watchdog_ms);
UNITREE_MOJO_EXPORT void unitree_mojo_destroy(unitree_mojo_client *client);
UNITREE_MOJO_EXPORT int32_t
unitree_mojo_is_ready(const unitree_mojo_client *client);

UNITREE_MOJO_EXPORT int32_t unitree_mojo_set_velocity(
    unitree_mojo_client *client, float vx, float vy, float vyaw);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_move_for(unitree_mojo_client *client,
                                                  float vx, float vy,
                                                  float vyaw,
                                                  uint32_t duration_ms);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_stop(unitree_mojo_client *client);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_stand_up(unitree_mojo_client *client);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_sit(unitree_mojo_client *client);
UNITREE_MOJO_EXPORT int32_t
unitree_mojo_stand_down(unitree_mojo_client *client);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_hello(unitree_mojo_client *client);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_dance(unitree_mojo_client *client);

UNITREE_MOJO_EXPORT int32_t unitree_mojo_get_state(
    const unitree_mojo_client *client, unitree_mojo_state *state);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_state_json(
    const unitree_mojo_client *client, char *buffer, size_t buffer_size);
UNITREE_MOJO_EXPORT int32_t unitree_mojo_last_error(
    const unitree_mojo_client *client, char *buffer, size_t buffer_size);

#ifdef __cplusplus
}
#endif

#endif

