# syntax=docker/dockerfile:1.6
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

COPY app.py .

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app
USER app

CMD ["python", "app.py"]
