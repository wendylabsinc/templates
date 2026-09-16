# llm-npu

Llama 3.2 3B answering questions on a **Qualcomm Hexagon NPU**, with a chat UI.
Runs entirely on the device — unplug the network mid-conversation and it keeps
answering.

```sh
wendy init --app-id my-assistant --template llm-npu --language python
cd my-assistant
wendy run --device <device>
```

Then open `http://<device>:3008/`.

## What you need

A device with a Qualcomm Hexagon NPU and an agent that grants the `npu`
entitlement — a **Dragonwing IQ-8275** on WendyOS 0.19.3 or later. Verified at
**~14.5 tokens/second** there.

It will not run on a device without that NPU. There is no CPU fallback: a 3B
model on eight Cortex-A78 cores manages about 3 tok/s, which is the difference
between an assistant and a wait.

## What comes from where

The `npu` entitlement injects only what an app **cannot obtain for itself** —
the FastRPC transport, which is locked to the host kernel's driver, plus the
board's DSP process-domain shells and its identity. It does not supply the
inference framework, and nothing is mounted over `/usr/lib`.

Everything else this app brings:

| | |
| --- | --- |
| QAIRT/Genie framework | downloaded from Qualcomm's public SDK **at build time** |
| `libQnnHtpV75Stub.so` + `libQnnHtpV75Skel.so` | the two arch-specific halves, from that SDK |
| Llama 3.2 3B QNN binaries (~2.6 GB) | downloaded **at first run** into the persist volume |

The SDK download makes the first build slow — a few minutes — and it is
layer-cached afterwards. To reuse a local copy:

```sh
wendy run --device <device> --build-arg QAIRT_URL=file:///path/to/v2.47.0.260601.zip
```

## Two things that are easy to get wrong

**The stub and the skel live in different trees of the SDK**, and Genie needs
both: it ignores the model bundle's declared `dsp_arch` and binds to the silicon
it actually finds. A copy loop that assumes one directory gets one and misses
the other, and the failure is `Failed to create device: 14001` — which names
neither the file nor the arch. The Dockerfile searches by filename and **fails
the build** if either is absent.

**The skel must sit on an image layer.** The persist volume holding the model is
mounted `noexec`, so a skel copied there cannot be loaded onto the DSP.

## The port opens before the model is ready

The 2.6 GB bundle takes minutes on a first run. An app that opens no port until
that finishes is indistinguishable from a hung one — the readiness probe times
out and the deploy is reported as failed while it is working correctly.

So the page is reachable from the first second and shows a real progress bar,
and `/progress` reports state and percentage. Nothing is reported ready until
the model has answered a warm-up prompt on the DSP, so a broken NPU path fails
at startup rather than mid-conversation.

## The conversation

History lives on the device, not in the browser: a reload rejoins the thread,
and everyone watching sees the same conversation.

`genie-t2t-run` is one-shot and keeps nothing between runs, so each turn
re-sends the recent conversation. Prompt processing runs at about 165 tok/s, so
an unbounded thread would make every answer slower than the last —
`HISTORY_TURNS` caps what the model sees at six messages. The page still shows
everything.

Only a real answer joins the thread. A cancelled or failed turn is not recorded,
so the model never learns that an empty reply is acceptable.

## Endpoints

| | |
| --- | --- |
| `/` | the chat page |
| `/ask?q=...` | one question, plain text — handy for scripts |
| `/progress` | state, download percentage, tokens per second |
| `/history` | the conversation as JSON |
| `/reset` | clear it |
| `/report.txt` | what the entitlement provided, and the warm-up measurements |

`/report.txt` is worth reading the first time: it shows exactly which files the
entitlement injected and which the app supplied, which is the fastest way to
diagnose a `14001`.
