FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[tts]" \
    && mkdir -p /app/audio/cache
COPY config.example.yaml ./config.yaml
ENV OPENFSD_CONFIG=/app/config.yaml
CMD ["openfsd-injector", "-c", "/app/config.yaml"]
