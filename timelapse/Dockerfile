FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests pillow
COPY timelapse.py /app/timelapse.py
VOLUME /data
CMD ["python", "-u", "/app/timelapse.py"]
