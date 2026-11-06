# llm-npu

Qwen3-4B answering questions on a **Qualcomm Hexagon NPU**, with a chat UI. After its
first run it works entirely on the device: unplug the network mid-conversation and it
keeps answering.

```sh
wendy init --app-id my-assistant --template llm-npu --language python
cd my-assistant
wendy run --device <device>
```

Then open `http://<device>:3008/`.

## What you need

A device with a Qualcomm Hexagon NPU and an agent that grants the `npu` entitlement:

| Board | DSP | Model bundle |
| --- | --- | --- |
| Dragonwing IQ-9075 | Hexagon V73 | `qcs9075` |
| Dragonwing IQ-8275 | Hexagon V75 | `qcs8275` |

The same image runs on both. It asks the DSP what it is at startup and fetches the
matching bundle. Any other DSP stops at startup with a message saying so, and there is
no CPU fallback.

## What comes from where

The `npu` entitlement injects only what an app **cannot obtain for itself**: the FastRPC
transport, which is locked to the host kernel's driver, plus the board's DSP
process-domain shells and its identity. It does not supply the inference framework, and
nothing is mounted over `/usr/lib`.

Everything else this app brings:

| | |
| --- | --- |
| QAIRT/Genie framework | downloaded from Qualcomm's public SDK **at build time**, pinned by SHA-256 |
| `libQnnHtpV73*` and `libQnnHtpV75*` stub + skel | the arch-specific halves, from that SDK |
| Qwen3-4B (Apache-2.0, ~2.5 GB) | Qualcomm AI Hub's Genie export, downloaded **at first run** into the persist volume, pinned by release and SHA-256 |

The SDK download makes the first build slow, a few minutes, and it is layer-cached
afterwards. To reuse a local copy:

```sh
wendy run --device <device> --build-arg QAIRT_URL=file:///path/to/v2.47.0.260601.zip
```

## Things that are easy to get wrong

**The stub and the skel live in different trees of the SDK**, and Genie needs both. It
binds to the silicon it finds, whatever the bundle declares. A missing pair fails as
`Failed to create device: 14001`, which names neither the file nor the arch. The SDK also
has a parallel `lib-safe/` tree with different builds of the same names, so the
Dockerfile copies from exact directories and **fails the build** if a file is absent.

**The skel must sit on an image layer.** The persist volume holding the model is mounted
`noexec`, so a skel copied there cannot be loaded onto the DSP.

**An NPU app must handle SIGTERM in PID 1.** The Agent stops one with SIGTERM to PID 1,
waits 30 seconds, and refuses to force-kill it. A shell entrypoint with no handler ignores
the signal, and the app can then be neither stopped nor replaced.

**Never signal a running Genie process.** SIGTERM to a live `genie-t2t-run` wedged an
IQ-9075's DSP until a power cycle. So `serve.py` waits for a turn in flight to finish and
delivers its answer before exiting, and `dialog::max-num-tokens` caps each answer at 200
tokens (`MAX_ANSWER_TOKENS`) so that it finishes inside the Agent's 30 seconds. A longer
answer is cut off at the cap.

## The port opens before the model is ready

The bundle takes minutes on a first run. An app that opens no port until that finishes is
indistinguishable from a hung one: the readiness probe times out and the deploy is
reported as failed while it is working correctly.

So the page is reachable from the first second and shows a real progress bar, and
`/progress` reports state and percentage. An interrupted download resumes, and the bundle
is checked against its hash before it is unpacked.

It reports `ready` only after the model has answered a warm-up prompt on the DSP. If that
fails, the state becomes `failed: <reason>`, the page says so, and `/ask` returns 503.

## The conversation

History lives on the device, not in the browser: a reload rejoins the thread, and everyone
watching sees the same conversation.

`genie-t2t-run` is one-shot and keeps nothing between runs, so each turn reloads the model
and re-sends the recent conversation. `HISTORY_TURNS` caps what the model sees at six
messages. The page still shows everything.

Only a real answer joins the thread. A failed turn is not recorded, so the model never
learns that an empty reply is acceptable.

## Endpoints

| | |
| --- | --- |
| `/` | the chat page |
| `/ask?q=...` | one question, plain text, handy for scripts |
| `/progress` | state, download percentage, tokens per second, board |
| `/history` | the conversation as JSON |
| `/reset` | clear it |
| `/report.txt` | what the entitlement provided, and the warm-up measurements |

`/report.txt` is worth reading the first time: it shows exactly which files the
entitlement injected and which the app supplied, which is the fastest way to diagnose a
`14001`.

## If the NPU gets stuck

A failed FastRPC run can wedge the DSP so that every later attempt fails for the wrong
reason, typically `Failed to create the dialog`. If `/report.txt` shows the warm-up failing
on a board that worked before, power-cycle the device. The persist volume survives, so the
model is not downloaded again.
