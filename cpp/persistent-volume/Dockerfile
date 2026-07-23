# syntax=docker/dockerfile:1.6
FROM arm64v8/debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY CMakeLists.txt main.cpp ./

RUN cmake -B build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build --config Release

FROM arm64v8/debian:bookworm-slim

COPY --from=builder /app/build/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}

CMD ["{{.APP_ID}}"]
