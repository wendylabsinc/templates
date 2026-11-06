# Qwen3-4B on the Hexagon NPU via Genie, from a container.
#
# The app bundles the QAIRT/Genie framework, because it can: the SDK is a public
# download, and pinning a version here is what makes this image reproducible. The npu
# entitlement supplies only what an app cannot obtain -- the FastRPC transport, locked to
# the host kernel's driver, and the board's PD shells and Hexagon C++ runtime. On a
# release image there is no SSH and no file-pull, so those are unobtainable by design.

# ---- builder: fetch the public SDK and keep only what this app runs ----
# Runs on the build host: this stage only downloads and copies files, so making it
# native avoids emulating a 2.3GB unzip.
FROM --platform=$BUILDPLATFORM debian:trixie-slim AS qairt
ARG QAIRT_VERSION=2.47.0.260601
ARG QAIRT_SHA256=d3497e110eae82c35a9152a93c0a18bbede402aaf9faa7a97c8079eb0f522b01
# Override to a file:// or http:// mirror to skip the 2.3GB fetch; the default is the
# same URL the OS recipe uses.
ARG QAIRT_URL=https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/${QAIRT_VERSION}/v${QAIRT_VERSION}.zip
# Served by the app, so the page still renders on a network with no internet.
ARG TAILWIND_VERSION=4.3.3
ARG TAILWIND_SHA256=a60c785630a06196808cbe79e6f7bdb4abcc8f4421a47b56f29338fc84805e3b

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fL --retry 3 -o /tmp/qairt.zip "$QAIRT_URL" \
    && echo "${QAIRT_SHA256}  /tmp/qairt.zip" | sha256sum -c - \
    && unzip -q /tmp/qairt.zip -d /tmp/sdk \
    && rm /tmp/qairt.zip

# Both DSP generations are carried, so one image runs on an IQ-9075 (Hexagon V73) and an
# IQ-8275 (V75). The arch is not negotiable: Genie loads the stub and skel matching the
# silicon it finds, and a missing pair surfaces at runtime only as "Failed to create
# device: 14001", which names neither the file nor the arch.
#
# The SDK carries a parallel lib-safe/ tree with different builds of the same names, so
# every file is taken from an exact directory rather than the first `find` hit. A missing
# file fails the build.
RUN set -eu; \
    sdk="$(echo /tmp/sdk/qairt/*)"; \
    lib="$sdk/lib/aarch64-oe-linux-gcc11.2"; bin="$sdk/bin/aarch64-oe-linux-gcc11.2"; \
    mkdir -p /out/lib /out/bin /out/dsp /out/web; \
    for f in libGenie libQnnHtp libQnnHtpV73Stub libQnnHtpV75Stub libQnnHtpPrepare \
             libQnnSystem libQairtHtpBackendExtensions libQnnHtpNetRunExtensions \
             libPlatformValidatorShared; do \
      cp -L "$lib/$f.so" /out/lib/; \
    done; \
    for b in genie-t2t-run qnn-platform-validator; do cp -L "$bin/$b" /out/bin/; done; \
    cp -L "$sdk/lib/hexagon-v73/unsigned/libQnnHtpV73Skel.so" /out/dsp/; \
    cp -L "$sdk/lib/hexagon-v75/unsigned/libQnnHtpV75Skel.so" /out/dsp/; \
    ls -l /out/lib /out/bin /out/dsp

RUN curl -fL --retry 3 -o /out/web/tailwind.js \
      "https://cdn.jsdelivr.net/npm/@tailwindcss/browser@${TAILWIND_VERSION}/dist/index.global.js" \
    && echo "${TAILWIND_SHA256}  /out/web/tailwind.js" | sha256sum -c -

# ---- runtime ----
FROM debian:trixie-slim

# libcdsprpc needs GLIBC_2.38 (bookworm ships 2.36) plus libyaml and libbsd.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 ca-certificates libatomic1 libgomp1 libstdc++6 libyaml-0-2 libbsd0 \
    && rm -rf /var/lib/apt/lists/*

# Kept off /usr/lib deliberately: the entitlement never writes there, and neither do we.
COPY --from=qairt /out/lib/ /opt/app/qairt/lib/
COPY --from=qairt /out/bin/ /opt/app/qairt/bin/

# The entitlement prepends its own prefix, so the host transport wins while the bundled
# framework stays ours.
ENV LD_LIBRARY_PATH=/opt/app/qairt/lib
ENV PATH=/opt/app/qairt/bin:$PATH

WORKDIR /app

# Genie finds the DSP skel through ADSP_LIBRARY_PATH, which serve.py points at /app/skel.
# It has to live on the image layer: the persist volume the model bundle lands in is
# mounted noexec, so a skel copied there cannot be loaded onto the DSP.
COPY --from=qairt /out/dsp/ /app/skel/

COPY --from=qairt /out/web/tailwind.js /app/
COPY report.sh serve.py index.html wendy-logo.svg /app/
EXPOSE {{.PORT}}
# Python is PID 1 on purpose. The Agent stops an NPU app with SIGTERM to PID 1 and refuses
# to force-kill it, so PID 1 must handle TERM and release the DSP (see serve.py).
CMD ["python3", "-u", "/app/serve.py"]
