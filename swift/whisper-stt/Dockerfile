# syntax=docker/dockerfile:1.6
# Stage 1: build libwhisper.so (CUDA) + the Swift binary + bake the GGML model.
# Base matches swift/llm-gguf (CUDA present for the whisper.cpp GGML_CUDA build).
FROM dustynv/tensorrt:8.6-r36.2.0 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates clang cmake curl git make ninja-build pkg-config \
    libcurl4-openssl-dev libedit2 libicu-dev libsqlite3-0 libxml2 \
    libncurses6 libstdc++6 libgcc-s1 \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Swift toolchain (aarch64) — same approach as swift/llm-gguf.
RUN mkdir -p /opt/swift \
 && curl -fSL https://download.swift.org/swift-6.2.3-release/ubuntu2204-aarch64/swift-6.2.3-RELEASE/swift-6.2.3-RELEASE-ubuntu22.04-aarch64.tar.gz \
    | tar -xz -C /opt/swift --strip-components=1
ENV PATH="/opt/swift/usr/bin:${PATH}"

# whisper.cpp — pinned clone (NOT a vendored tarball), CUDA build, shared libs.
ARG WHISPER_CPP_TAG=v1.7.4
ARG CUDA_ARCH=87
RUN git clone --depth 1 --branch ${WHISPER_CPP_TAG} \
      https://github.com/ggerganov/whisper.cpp /opt/whisper.cpp \
 && cmake -S /opt/whisper.cpp -B /opt/whisper.cpp/build -G Ninja \
      -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
      -DBUILD_SHARED_LIBS=ON \
      -DWHISPER_BUILD_EXAMPLES=OFF \
      -DWHISPER_BUILD_TESTS=OFF \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -lcuda" \
 && cmake --build /opt/whisper.cpp/build -j"$(nproc)" \
 && cmake --install /opt/whisper.cpp/build \
 && ldconfig

# Bake the GGML model into the image (default base.en).
ARG WHISPER_MODEL=base.en
RUN mkdir -p /opt/whisper/models \
 && bash /opt/whisper.cpp/models/download-ggml-model.sh ${WHISPER_MODEL} /opt/whisper/models

WORKDIR /app
COPY Package.swift ./
COPY Sources ./Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}},target=/app/.build \
    swift build -c release \
      -Xcc -I/usr/local/include \
      -Xlinker -L/usr/local/lib \
 && cp .build/release/{{.APP_ID}} /app/{{.APP_ID}}

# Stage 2: slim runtime — Swift runtime + whisper/ggml libs + GStreamer/ALSA + model.
FROM dustynv/tensorrt:8.6-r36.2.0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libcurl4 libedit2 libicu70 libxml2 \
    libncurses6 libstdc++6 libgcc-s1 \
    libgstreamer1.0-0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-alsa alsa-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/swift/usr/lib/swift /opt/swift/usr/lib/swift
COPY --from=builder /usr/local/lib/libwhisper.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libggml*.so* /usr/local/lib/
COPY --from=builder /opt/whisper/models /opt/whisper/models
COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
RUN ldconfig

ENV LD_LIBRARY_PATH="/opt/swift/usr/lib/swift/linux:/usr/local/lib:${LD_LIBRARY_PATH}"
ENV WHISPER_MODEL={{.WHISPER_MODEL}}
ENV WHISPER_MODEL_PATH=/opt/whisper/models/ggml-{{.WHISPER_MODEL}}.bin
ENV TRANSCRIPT_FILE=/data/transcript.txt

CMD ["{{.APP_ID}}"]
