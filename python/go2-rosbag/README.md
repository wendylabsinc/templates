# {{.APP_ID}}

A browser-based topic inspector and MCAP recorder for the Unitree Go2. It
discovers native Go2 DDS topics with Unitree type support, displays schemas and
samples, records all or selected topics, and downloads completed bags.

## When to use this template

For one-off recording from a normal ROS 2 graph, use
`wendy device ros2 bag record`. Use this project when you need its persistent web
UI, Go2 message packages, topic inspection, selected-topic workflow, or source
code to customize recording.

## Requirements

- A Unitree Go2 EDU and a WendyOS computer on the robot DDS network
- Persistent storage with enough free space for the chosen topics and duration
- Network access during the first build for ROS 2, CycloneDDS, Unitree message
  packages, and MCAP support

High-rate LiDAR and image topics can grow bags quickly. Watch the live size and
stop recording when the needed interval is complete.

## Run and verify

```sh
wendy run
```

Open `http://<go2-hostname>:{{.PORT}}`. Confirm that topics and types appear,
inspect one topic, start a short selected-topic recording, stop it, and download
the resulting MCAP file.

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `PORT` | `7000` | Web UI and API port |
| `GO2_IP` | `192.168.123.161` | Address used to select the host interface that reaches the robot network |
| `ROS_DOMAIN_ID` | `0` | Runtime DDS domain set in `recorder/Dockerfile` |
| `AUTO_RECORD` | `0` | Set to `1` to begin an all-topic recording at startup |

Use `wendy run --env ROS_DOMAIN_ID=<id>` or `--env AUTO_RECORD=1` for temporary
runtime overrides.

## How it works

The single `recorder` service uses host networking and mounts the `rosbags`
persistent volume at `/data`. `recorder/entrypoint.sh` determines the local
interface that routes to `GO2_IP` and configures CycloneDDS.
`recorder/server.py` wraps ROS 2 CLI operations and the recorder process.

| API | Purpose |
|---|---|
| `GET /api/topics` | Topic and type list |
| `GET /api/topic?name=/x` | Type, schema, endpoints, QoS, and one sample |
| `GET /api/hz?name=/x` | Short publish-rate measurement |
| `POST /api/record/start` | Start all topics or a JSON `topics` list |
| `POST /api/record/stop` | Stop and finalize the current bag |
| `GET /api/bags` and `GET /download` | List and download recordings |

## Extend it

- Add inspection or recording routes in `recorder/server.py`.
- Add required ROS 2 or Unitree packages in `recorder/Dockerfile`.
- Change DDS interface selection in `recorder/entrypoint.sh`.
- Add retention or upload behavior around completed files in `/data`.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --service recorder --tail 200
wendy device apps stop {{.APP_ID}}
```

If the topic list is empty, check the selected local interface, robot network,
and domain ID. If inspection fails for only one topic, its type support or QoS
may differ from the installed packages. Stop recording through the UI before
stopping the app so rosbag can finalize its metadata.
