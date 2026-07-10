FROM ros:humble-ros-base
# rmw-cyclonedds-cpp is REQUIRED: the Go2 speaks CycloneDDS, and
# RMW_IMPLEMENTATION below selects it — without the package rosbag2 can't
# load the RMW and won't discover the robot's topics.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-humble-rosbag2-storage-mcap ros-humble-rmw-cyclonedds-cpp python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir fastapi uvicorn
COPY cyclonedds.xml /opt/cyclonedds.xml
COPY app.py /opt/app.py
# Runtime config (baked at scaffold time from template.json — no compose file).
# PORT is internal: the console reaches the recorder at http://127.0.0.1:3610.
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    CYCLONEDDS_URI=/opt/cyclonedds.xml \
    PORT=3610 \
    RECORD_TOPICS="{{.LIDAR_TOPIC}} {{.IMU_TOPIC}}" \
    BAG_DIR=/data/bags

EXPOSE 3610
CMD ["bash", "-c", "source /opt/ros/humble/setup.bash && uvicorn app:app --app-dir /opt --host 0.0.0.0 --port ${PORT:-3610}"]
