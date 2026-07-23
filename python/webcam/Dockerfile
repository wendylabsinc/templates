# Use lightweight Debian base with GStreamer support
FROM debian:bookworm-slim

# Flush stdout/stderr immediately so logs appear in `wendy run` output.
ENV PYTHONUNBUFFERED=1

# Install GStreamer and Python dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-nice \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies in a venv
RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY index.html .
COPY logo.svg .

# Create a non-root user for security and fix permissions
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app && \
    chmod -R u+r /app && \
    usermod -aG video app
USER app

# Expose port {{.PORT}}
EXPOSE {{.PORT}}

# Run the application with uvicorn
CMD ["/app/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
