# go2-console — no ROS needed: talks DDS directly via cyclonedds-python
# (same trick as go2-foxglove's bridge) and pure-numpy export math.
FROM python:3.11-slim
RUN pip install --no-cache-dir fastapi uvicorn httpx numpy cyclonedds pillow
WORKDIR /opt/console
COPY app.py export.py nogo.py quality.py ./
COPY static ./static
COPY cyclonedds.xml /opt/cyclonedds.xml

# Runtime config (baked at scaffold time from template.json — no compose file).
ENV CYCLONEDDS_URI=/opt/cyclonedds.xml \
    PORT={{.CONSOLE_PORT}} \
    RECORDER_URL=http://127.0.0.1:3610 \
    MAP_SAVE_DIR=/data/maps \
    EXPORT_DIR=/data/exports \
    FOXGLOVE_PORT={{.FOXGLOVE_PORT}} \
    SLICE_MIN_M={{.SLICE_MIN_M}} \
    SLICE_MAX_M={{.SLICE_MAX_M}} \
    GRID_RESOLUTION_M={{.GRID_RESOLUTION_M}} \
    LIDAR_HEIGHT_OFFSET_M={{.LIDAR_HEIGHT_OFFSET_M}}

EXPOSE {{.CONSOLE_PORT}}
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-3600}"]
