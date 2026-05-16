#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void *RealSenseBridgeRef;

typedef struct RealSenseBridgeFrame {
    uint8_t *data;
    size_t length;
    uint64_t sequence;
} RealSenseBridgeFrame;

RealSenseBridgeRef RealSenseBridgeCreate(void);
void RealSenseBridgeDestroy(RealSenseBridgeRef bridge);

bool RealSenseBridgeStart(RealSenseBridgeRef bridge, char *error, size_t errorLength);
void RealSenseBridgeStop(RealSenseBridgeRef bridge);
bool RealSenseBridgeIsRunning(RealSenseBridgeRef bridge);

bool RealSenseBridgeConfigure(
    RealSenseBridgeRef bridge,
    int width,
    int height,
    int fps,
    const char *preset,
    char *error,
    size_t errorLength
);

char *RealSenseBridgeHealthJSON(RealSenseBridgeRef bridge);
void RealSenseBridgeFreeString(char *string);

bool RealSenseBridgeIsKnownStream(const char *streamID);
bool RealSenseBridgeWaitFrame(
    RealSenseBridgeRef bridge,
    const char *streamID,
    uint64_t lastSequence,
    int timeoutMilliseconds,
    RealSenseBridgeFrame *frame
);
void RealSenseBridgeFreeFrame(RealSenseBridgeFrame *frame);

#ifdef __cplusplus
}
#endif
