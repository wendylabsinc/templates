FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

ARG WENDY_DEVICE_HOSTNAME=localhost
ENV WENDY_DEVICE_HOSTNAME=${WENDY_DEVICE_HOSTNAME}

RUN uv pip install --system fastapi==0.135.3 "uvicorn[standard]"

COPY app.py .

EXPOSE 3001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3001"]
