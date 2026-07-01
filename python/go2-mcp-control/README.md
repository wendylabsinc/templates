# go2-mcp-control

Drive the **Unitree Go2 EDU** in **natural language** through Claude. Built as a
**multi-service app group**: two single-responsibility containers defined by one
native `wendy.json` `services` map.

```
go2-mcp-control/
├── wendy.json     ← native multi-service app group (2 services)
├── motion/        ← FastAPI motion API (:3201) — the only process linking the Unitree SDK
└── mcp/           ← FastMCP server (:{{.MCP_PORT}}) over HTTP — exposes the dog as Claude tools
```

You register the MCP server with the Claude CLI, then just talk: *"stand up",
*"walk forward for 3 seconds"*, *"turn left"*, *"how's your battery?"*, *"sit
down"*. Claude picks the right tool and calls the dog.

> This is the **real dog's own motion repertoire** driven over DDS. There is no
> Nav2/SLAM here (the Go2 walks itself, it does not autonomously map or
> navigate), so there are deliberately no explore/inspect/zone tools.

## Architecture

```
                      Claude (CLI / desktop)
                              │  MCP over HTTP
                              │  http://<device>:{{.MCP_PORT}}/mcp
                              ▼
   ┌──────────────────────────────────────────────┐
   │  mcp   FastMCP server (streamable HTTP)        │
   │  ─ one tool per motion action                  │
   │  ─ forwards each call → 127.0.0.1:3201         │
   └───────────────────────┬────────────────────────┘
                           │ HTTP
                  127.0.0.1:3201
                           ▼
   ┌───────────────────────────────┐
   │  motion  SportClient           │
   │  drives the dog over           │
   │  CycloneDDS on {{.NETWORK_INTERFACE}}   │
   │  velocity caps + watchdog here │
   └───────────────┬────────────────┘
                   │
                   ▼
             Unitree Go2 EDU
           (192.168.123.0/24)
```

Both services talk over **localhost** and reach the physical dog over the
**`192.168.123.0/24`** network — made possible by the `network: host`
entitlement each declares. The `mcp` service links no DDS/SDK at all; it is a
thin FastMCP→HTTP bridge, so it stays a lean `python:3.11-slim` image. The
`motion` service is copied verbatim from the `go2-rc` template — same
`SportClient` wrapper, same safety caps.

## Services

| Service  | Port | Role |
| -------- | ---- | ---- |
| `motion` | 3201 | FastAPI control plane — `/velocity`, `/move`, `/stop`, `/stand`, `/sit`, `/lie`, `/hello`, `/dance`, `/state`. The only container linking `unitree_sdk2_python`'s `SportClient`; velocity clamps + 1 s watchdog live here. |
| `mcp`    | `{{.MCP_PORT}}` | FastMCP server over streamable HTTP at `/mcp`. Exposes the tools below and forwards each to `motion`. Starts after `motion`. |

## MCP tools

| Tool | Say something like | Action |
|------|--------------------|--------|
| `stand_up` | "stand up" | Stand on all fours (ready posture). |
| `sit_down` | "sit" | Sit down (resting). |
| `lie_down` | "lie down" | Lie down (lowest posture). |
| `wave_hello` | "say hello" / "wave" | Front-paw wave gesture. |
| `dance` | "dance" | Built-in dance routine. |
| `move` | "walk forward 3 seconds" / "turn left" / "strafe right" | Walk a direction (forward/backward/left/right/turn_left/turn_right) for N seconds (0.1–10), then auto-stop. |
| `set_velocity` | (advanced) "creep forward slowly" | Low-level non-blocking vx/vy/vyaw command (watchdog-guarded). |
| `stop` | "stop" / "halt" | Halt all motion immediately. |
| `get_state` | "how's your battery?" / "are you level?" | Battery %, voltage, IMU rpy, foot forces. |
| `get_health` | "are you connected?" | Is the motion service up + SportClient connected. |

## Configure

This template prompts for:

- **APP_ID** — the app group identifier.
- **MCP_PORT** (default `3450`) — where the FastMCP HTTP server listens.
- **GO2_IP** (default `192.168.123.161`) — the Go2 main-controller IP.
- **NETWORK_INTERFACE** (default `eth0`) — the NIC on the deploy host that
  carries the `192.168.123.0/24` address. On the Go2's onboard Jetson this is
  `eth0`.

## Deploy

```sh
wendy init --template go2-mcp-control
cd <app-id>
wendy run --device <go2-jetson>
```

## Connect Claude

Once the app is running on the dog, point the Claude CLI at the MCP server
(streamable HTTP transport, `/mcp` path):

```sh
claude mcp add --transport http go2 http://<go2-jetson>:{{.MCP_PORT}}/mcp
```

Then talk to Claude: *"stand up and walk forward for 2 seconds, then turn left
and tell me your battery level."* Claude will call `stand_up`, `move`, `move`,
then `get_state`.

To sanity-check the endpoint without Claude:

```sh
curl -i http://<go2-jetson>:{{.MCP_PORT}}/mcp    # streamable-HTTP endpoint responds
```

> **Safety:** the `motion` service enforces velocity caps (vx≤0.6, vy≤0.4,
> vyaw≤1.0) and a 1-second watchdog next to the hardware — if commands stop
> arriving, the dog stops. These live in `motion/go2_controller.py` and are
> inherited from `go2-rc`. Keep a clear area around the robot and be ready to
> say **"stop"** (or hit Ctrl-C on the motion container).
