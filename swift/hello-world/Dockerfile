FROM swift:6.3-bookworm AS builder

ARG SWIFT_WASM_SDK_ID=swift-6.3.2-RELEASE_wasm-embedded
ARG SWIFT_WASM_SDK_URL=https://download.swift.org/swift-6.3.2-release/wasm-sdk/swift-6.3.2-RELEASE/swift-6.3.2-RELEASE_wasm.artifactbundle.tar.gz
ARG SWIFT_WASM_SDK_CHECKSUM=a61f0584c93283589f8b2f42db05c1f9a182b506c2957271402992655591dd7c

RUN swift sdk install "${SWIFT_WASM_SDK_URL}" --checksum "${SWIFT_WASM_SDK_CHECKSUM}"

WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN swift build --swift-sdk "${SWIFT_WASM_SDK_ID}" -c release \
    && cp ".build/wasm32-unknown-wasip1/release/{{.APP_ID}}.wasm" "/tmp/{{.APP_ID}}.wasm"

FROM scratch
COPY --from=builder /tmp/{{.APP_ID}}.wasm /{{.APP_ID}}.wasm
