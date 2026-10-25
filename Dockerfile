# syntax=docker/dockerfile:1
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Transcribe" \
      org.opencontainers.image.description="Self-hosted audio to text with faster-whisper. Hebrew-first." \
      org.opencontainers.image.source="https://github.com/eran-yadin/transcribe" \
      org.opencontainers.image.licenses="MIT"

# HF_HOME points into the data volume so the ~1.6GB model is downloaded once and
# survives an image update.
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/data/models

WORKDIR /app

# PyAV (bundled ffmpeg) and ctranslate2 ship manylinux wheels, so no apt build
# deps are needed. ffmpeg is here only for robustness with odd container formats.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

# Runs as root deliberately. A fresh docker named volume is created root-owned,
# so a non-root UID cannot write /data and SQLite reports "attempt to write a
# readonly database". Changing this means adding a chown init step first.
#
# OMP_NUM_THREADS is set at runtime (see compose.yml), not here: as an ENV with
# no matching ARG it would bake one fixed value into the image at build time.
EXPOSE 8011
CMD ["python", "app.py"]
