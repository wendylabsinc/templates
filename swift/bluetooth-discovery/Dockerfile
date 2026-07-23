# Build Swift server with D-Bus (BlueZ backend of wendylabsinc/bluetooth links libdbus-1)
FROM swift:6.2.3-noble AS swift-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libdbus-1-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Package.swift Package.resolved* ./
COPY Sources ./Sources

RUN swift build

# Runtime stage
FROM swift:6.2.3-noble-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libdbus-1-3 \
    bluez \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=swift-builder /app/.build/debug/BluetoothDiscovery /usr/local/bin/bluetooth-discovery
COPY index.html .
COPY logo.svg .

EXPOSE {{.PORT}}

CMD ["bluetooth-discovery"]
