# llm-chat

Local chat UI for WendyOS devices. The Python backend launches `llama-server`
and proxies chat requests to its OpenAI-compatible API.

## Model selection

The template defaults to `GEMMA_MODEL=auto`:

- `nano`: `ggml-org/gemma-4-E2B-it-GGUF:Q8_0`, selected below 12 GiB RAM.
- `orin`: `ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M`, selected from 12 GiB to 48 GiB RAM.
- `agx-orin` / `thor`: `ggml-org/gemma-4-26B-A4B-it-GGUF:Q4_K_M`, selected at 48 GiB RAM and above.

Override at scaffold time:

```bash
wendy init \
  --app-id llm-chat-demo \
  --target wendyos \
  --language python \
  --template llm-chat \
  --var PORT=3010 \
  --var GEMMA_MODEL=orin \
  --assistant skip \
  --git-init no
```

Override at runtime by setting `GEMMA_MODEL` to any Hugging Face GGUF repo
selector, for example `ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M`, or set
`LLAMA_MODEL_PATH` to a local `.gguf` file.

Model downloads are cached under the `/models` persist volume.
The Docker image preloads the Nano GGUF by default so USB-C/link-local device
deploys do not depend on outbound Hugging Face access from the device.
`LLAMA_REASONING=off` is the default so Gemma 4 returns chat text instead of
spending the response budget in `reasoning_content`.
