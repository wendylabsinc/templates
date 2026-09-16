# Llama 3.2 3B on the Hexagon NPU via Genie, from a container.
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

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fL --retry 3 -o /tmp/qairt.zip "$QAIRT_URL" \
    && echo "${QAIRT_SHA256}  /tmp/qairt.zip" | sha256sum -c - \
    && unzip -q /tmp/qairt.zip -d /tmp/sdk \
    && rm /tmp/qairt.zip

# aarch64-oe-linux-gcc11.2 is the target triple for this board's userspace; the DSP half
# of the runtime is a separate hexagon-v75 tree. Both halves are needed, and the arch is
# not negotiable: Genie ignores the bundle's dsp_arch ("Specified config ARCH, ignoring on
# real target") and loads the stub and skel matching the silicon it finds.
#
# Each file is searched for by name rather than assumed to share one directory, and an
# absent one fails the build. A skipped copy is not survivable and does not stay quiet --
# it resurfaces at runtime as "Failed to create device: 14001", which names neither the
# file nor the arch.
RUN set -eu; \
    mkdir -p /out/lib /out/bin /out/dsp; \
    for f in libGenie libQnnHtp libQnnHtpV75Stub libQnnHtpPrepare libQnnSystem \
             libQairtHtpBackendExtensions libQnnHtpNetRunExtensions; do \
      p="$(find /tmp/sdk -path '*aarch64-oe-linux-gcc11.2*' -name "$f.so" | head -1)"; \
      [ -n "$p" ] || { echo "required $f.so is not in the SDK" >&2; exit 1; }; \
      cp -L "$p" /out/lib/; \
    done; \
    skel="$(find /tmp/sdk -path '*hexagon-v75*' -name 'libQnnHtpV75Skel.so' | head -1)"; \
    [ -n "$skel" ] || { echo "required libQnnHtpV75Skel.so is not in the SDK" >&2; exit 1; }; \
    cp -L "$skel" /out/dsp/; \
    for b in genie-t2t-run qnn-platform-validator; do \
      p="$(find /tmp/sdk -path '*aarch64-oe-linux-gcc11.2*' -name "$b" -type f | head -1)"; \
      [ -n "$p" ] || { echo "required $b is not in the SDK" >&2; exit 1; }; \
      cp -L "$p" /out/bin/; \
    done; \
    v="$(find /tmp/sdk -path '*aarch64-oe-linux-gcc11.2*' -name libPlatformValidatorShared.so | head -1)"; \
    if [ -n "$v" ]; then cp -L "$v" /out/lib/; fi; \
    ls -l /out/lib /out/bin /out/dsp

# ---- runtime ----
FROM debian:trixie-slim

# libcdsprpc needs GLIBC_2.38 (bookworm ships 2.36) plus libyaml and libbsd.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 curl ca-certificates libatomic1 libgomp1 libstdc++6 libyaml-0-2 libbsd0 \
    && rm -rf /var/lib/apt/lists/*

# Kept off /usr/lib deliberately: the entitlement never writes there, and neither do we.
COPY --from=qairt /out/lib/ /opt/app/qairt/lib/
COPY --from=qairt /out/bin/ /opt/app/qairt/bin/

# The entitlement prepends its own prefix, so the host transport wins while the bundled
# framework stays ours.
ENV LD_LIBRARY_PATH=/opt/app/qairt/lib
ENV PATH=/opt/app/qairt/bin:$PATH

# The demo contract for Wendy voice (wendy-voice docs/MAKE-YOUR-DEMO-VISIBLE.md).
# Pinned below 2.x: contract.py reads the tool list through FastMCP's private
# _tool_manager, which is only guaranteed within the 1.x line.
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && pip3 install --no-cache-dir --break-system-packages "mcp[cli]<2" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Genie finds the DSP skel through ADSP_LIBRARY_PATH, which both run.sh and serve.py point
# at /app/skel. It has to live on the image layer: the persist volume the model bundle
# lands in is mounted noexec, so a skel copied there cannot be loaded onto the DSP.
COPY --from=qairt /out/dsp/ /app/skel/

COPY run.sh prompt.txt serve.py wendy-logo.svg /app/
RUN chmod +x /app/run.sh
EXPOSE {{.PORT}}
CMD ["/app/run.sh"]
