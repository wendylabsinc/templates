import os
from pathlib import Path

from huggingface_hub import hf_hub_download
from llama_cpp import Llama

REPO_ID = "unsloth/gemma-4-E4B-it-GGUF"
MODEL_FILENAME = "gemma-4-E4B-it-Q3_K_M.gguf"
MMPROJ_FILENAME = "mmproj-F16.gguf"
MODELS_DIR = Path(
    os.environ.get("ALBERT_MODELS_DIR", Path.home() / ".albert" / "models")
)


def _ensure_downloaded(filename: str) -> Path:
    path = MODELS_DIR / filename
    if not path.exists():
        print(f"Downloading {REPO_ID}/{filename} ...")
        downloaded = hf_hub_download(
            repo_id=REPO_ID,
            filename=filename,
            local_dir=str(MODELS_DIR),
        )
        path = Path(downloaded)
        print(f"Saved to {path}")
    return path


class LocalLLM:
    def __init__(self):
        self._llm: Llama | None = None

    def _ensure_loaded(self):
        if self._llm is not None:
            return

        model_path = _ensure_downloaded(MODEL_FILENAME)

        self._llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=-1,
            n_ctx=4096,
            verbose=False,
        )

    def chat(self, messages: list[dict], max_tokens: int = 512) -> str:
        self._ensure_loaded()
        assert self._llm is not None

        response = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
            top_p=0.9,
            repeat_penalty=1.1,
        )
        content = response["choices"][0]["message"]["content"]
        return (content or "").strip()

    def chat_with_image(
        self, prompt: str, image_base64: str, max_tokens: int = 256
    ) -> str:
        """Send a prompt with an image to the multimodal model."""
        self._ensure_loaded()
        assert self._llm is not None

        response = self._llm.create_chat_completion(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return response["choices"][0]["message"]["content"]

    def generate_title(self, first_message: str) -> str:
        return self.chat(
            [
                {
                    "role": "system",
                    "content": "Generate a short title (5 words max) for a conversation that starts with the following message. Reply with ONLY the title, nothing else.",
                },
                {"role": "user", "content": first_message},
            ],
            max_tokens=20,
        )


# Singleton — model loads on first use
llm = LocalLLM()
