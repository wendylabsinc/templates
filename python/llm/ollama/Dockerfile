# syntax=docker/dockerfile:1.6
#
# The base Ollama image is selected at `wendy run` time from build args the
# wendy CLI injects after querying the target device (see Wendy docs on
# injected build args):
#   WENDY_GPU_VENDOR=nvidia + WENDY_JETPACK_MAJOR=6
#       -> stock ollama/ollama with its bundled JetPack-6 CUDA libs forced on
#   anything else (JetPack 7 / Thor, x86_64 CUDA where present, otherwise CPU,
#   and plain `docker build` with no args) -> stock ollama/ollama
#
# Unlike the camera-feed-yolo templates, no CUDA-12-runtime/LD_LIBRARY_PATH
# shim is needed here: that fix is for JetPack-6 images running on JetPack-7
# hosts, whereas stock ollama ships its own CUDA backends and picks the one
# matching the host at startup.
FROM ollama/ollama:latest AS final
ARG WENDY_GPU_VENDOR=generic
ARG WENDY_JETPACK_MAJOR=0

# Every vendor uses the stock image. Only JetPack 6 needs an explicit backend
# hint; vendor discovery may report values such as broadcom, arm, or qualcomm.
RUN if [ "$WENDY_GPU_VENDOR" = nvidia ] && [ "$WENDY_JETPACK_MAJOR" = 6 ]; then \
        touch /etc/wendy-jetpack6; \
    fi

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# OLLAMA_LOAD_TIMEOUT: the 5m default aborts cold loads of large models
# (e.g. Laguna S 2.1's ~75GB weights take >5m to read from disk on Jetson).
ENV OLLAMA_HOST="0.0.0.0:11434" \
    OLLAMA_MODELS="/models/ollama" \
    OLLAMA_MODEL="gemma4:e2b" \
    OLLAMA_LOAD_TIMEOUT="20m"

RUN mkdir -p /models/ollama

COPY entrypoint.sh /usr/local/bin/wendy-ollama-entrypoint
RUN chmod 0755 /usr/local/bin/wendy-ollama-entrypoint

EXPOSE 11434

ENTRYPOINT []
CMD ["wendy-ollama-entrypoint"]
