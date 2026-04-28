FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

{{if eq .INFERENCE_MODE "local"}}
RUN apt-get update && apt-get install -y --no-install-recommends \
    portaudio19-dev \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*
{{end}}

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

ENV PORT={{.PORT}}
{{if eq .INFERENCE_MODE "local"}}
ENV INFERENCE_MODE=local
ENV LOCAL_MODEL={{.LOCAL_MODEL}}
{{end}}
{{if eq .INFERENCE_MODE "cloud"}}
ENV INFERENCE_MODE=cloud
ENV CLOUD_PROVIDER={{.CLOUD_PROVIDER}}
{{end}}

EXPOSE {{.PORT}}
CMD ["uv", "run", "{{.APP_ID}}"]
