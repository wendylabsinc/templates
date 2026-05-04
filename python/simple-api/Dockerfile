FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

ARG WENDY_DEVICE_HOSTNAME=localhost
ENV WENDY_DEVICE_HOSTNAME=${WENDY_DEVICE_HOSTNAME}

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}

RUN uv pip install --system fastapi==0.135.3 "uvicorn[standard]"
RUN if [ "$WENDY_DEBUG" = "true" ]; then uv pip install --system debugpy; fi

COPY app.py .

EXPOSE {{.PORT}}

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
