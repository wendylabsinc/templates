#ifndef CONNXRUNTIME_SHIM_H
#define CONNXRUNTIME_SHIM_H

// The Microsoft tarball installs onnxruntime_c_api.h in /usr/local/include
// (no subdirectory). The dustynv/onnxruntime image places it under
// /usr/local/include/onnxruntime/. The Dockerfile copies/symlinks both into
// /usr/local/include so this single include works on every base.
#include <onnxruntime_c_api.h>

#endif
