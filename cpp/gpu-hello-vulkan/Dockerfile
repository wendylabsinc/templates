# Vulkan GPU diagnostics. The `gpu` entitlement in wendy.json is the ONLY setup
# required: Wendy injects the device's render node and this image brings its own
# userspace driver.
#
# Mesa version matters. Adreno A623 (SA8775P/QCS8300) needs Mesa >= 26 for the
# Turnip driver to recognise it -- bookworm's 22.3 segfaults in vkCreateInstance
# and trixie's 25.0.7 returns VK_ERROR_INCOMPATIBLE_DRIVER. sid ships 26.2.2.
FROM debian:sid-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
      g++ libvulkan-dev glslc ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY main.cpp vecadd.comp matmul.comp ./
RUN glslc -O vecadd.comp -o vecadd.spv \
 && glslc -O matmul.comp -o matmul.spv \
 && g++ -O2 -std=c++17 main.cpp -o gpu-hello -lvulkan

FROM debian:sid-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libvulkan1 mesa-vulkan-drivers python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /src/gpu-hello /app/gpu-hello
COPY --from=build /src/vecadd.spv /src/matmul.spv /app/
COPY entrypoint.sh index.html /app/
RUN chmod +x /app/entrypoint.sh

ENV XDG_RUNTIME_DIR=/tmp
CMD ["/app/entrypoint.sh"]
