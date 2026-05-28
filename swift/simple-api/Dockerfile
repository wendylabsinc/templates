FROM swift:6.3-bookworm AS builder

WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN swift build -c release

FROM swift:6.3-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.build/release/{{.APP_ID}} /app/{{.APP_ID}}

EXPOSE {{.PORT}}

ENTRYPOINT ["/app/{{.APP_ID}}"]
