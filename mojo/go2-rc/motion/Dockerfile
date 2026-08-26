# syntax=docker/dockerfile:1.6
FROM ubuntu:22.04 AS builder

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake git libboost-all-dev libeigen3-dev \
      libfmt-dev libyaml-cpp-dev python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir modular==26.5.0

ARG UNITREE_SDK2_REF=9754cd153af3da471b0fe5f3aa535e426fb11db3
ARG UNITREE_MOJO_REF=v0.2.0
RUN git clone https://github.com/unitreerobotics/unitree_sdk2.git /opt/unitree_sdk2 \
    && git -C /opt/unitree_sdk2 checkout "${UNITREE_SDK2_REF}"
RUN git clone --depth 1 --branch "${UNITREE_MOJO_REF}" \
      https://github.com/wendylabsinc/unitree-mojo.git /opt/unitree-mojo

WORKDIR /build
RUN cmake -S /opt/unitree-mojo -B native \
      -DUNITREE_SDK2_DIR=/opt/unitree_sdk2 \
      -DBUILD_EXAMPLES=OFF \
      -DCMAKE_BUILD_TYPE=Release \
    && cmake --build native --parallel \
    && mkdir -p /build/dds \
    && cp /opt/unitree_sdk2/thirdparty/lib/$(uname -m)/libddsc*.so /build/dds/ \
    && ln -s libddsc.so /build/dds/libddsc.so.0 \
    && ln -s libddscxx.so /build/dds/libddscxx.so.0

COPY main.mojo .
COPY wendynet wendynet
RUN mojo build -I /opt/unitree-mojo -I /build -o /build/go2-motion main.mojo
RUN mkdir -p /build/runtime \
    && find /usr/local/lib/python3.10/dist-packages/modular/lib \
       -maxdepth 1 -type f \
       \( -name 'libKGENCompilerRTShared.so' \
          -o -name 'libMSupportGlobals.so' \
          -o -name 'libAsyncRTRuntimeGlobals.so' \) \
       -exec cp {} /build/runtime/ \;

FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/go2-motion /app/go2-motion
COPY --from=builder /build/runtime /app/runtime
COPY --from=builder /build/native/libunitree_mojo.so /app/lib/libunitree_mojo.so
COPY --from=builder /build/dds /app/lib

ENV LD_LIBRARY_PATH=/app/lib:/app/runtime \
    GO2_NETWORK_INTERFACE={{.NETWORK_INTERFACE}}
EXPOSE 3201
CMD ["/app/go2-motion"]
