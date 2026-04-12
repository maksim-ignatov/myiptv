FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    libavcodec-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN mkdir -p videos data assets && \
    chmod +x stream.py && \
    find assets -type f -exec chmod 644 {} \;

CMD ["python3", "-u", "stream.py"]
