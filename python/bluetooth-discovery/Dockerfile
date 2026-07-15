# Use uv with Python 3.14 (slim variant for better compatibility)
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Install BlueZ dependencies for Bluetooth support
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    libdbus-1-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies at build time (not runtime)
RUN uv pip install --system -r requirements.txt

# Copy application files
COPY app.py .
COPY index.html .
COPY logo.svg .

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app
USER app

# Expose the app port
EXPOSE {{.PORT}}

# Run the application with uvicorn (dependencies already installed)
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
