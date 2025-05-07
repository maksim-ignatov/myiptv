FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN mkdir -p videos && \
    chmod +x stream.py

CMD ["python3", "-u", "stream.py"] 