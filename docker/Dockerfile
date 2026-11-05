# OpenMontage video-generator API - CPU-only image.
#
#   docker build -f docker/Dockerfile -t openmontage-api .
#   docker run --init --shm-size=2g -p 8710:8080 --env-file .env -v openmontage-data:/data openmontage-api
#
# The container listens on 8080 internally; map whatever host port suits you (-p HOST:8080).
#
# Ships the full video API: FastAPI job layer + one unattended Claude Agent SDK session
# per video job (the Claude Code runtime is installed below) + Remotion rendering.

FROM node:22-bookworm-slim AS node

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENMONTAGE_DATA_DIR=/data

# ffmpeg + the shared libraries Chrome Headless Shell (Remotion) needs on Debian, plus emoji fonts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates curl fonts-noto-color-emoji fonts-dejavu-core libgl1 libglib2.0-0 \
      libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 libxrandr2 libxkbcommon-dev libxfixes3 \
      libxcomposite1 libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libcups2 \
 && rm -rf /var/lib/apt/lists/*

# Node 22 from the official image (only node + npm; python keeps /usr/local intact).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# The Claude Code runtime the Agent SDK drives (one headless session per video job).
RUN npm install -g --no-audit --no-fund @anthropic-ai/claude-code \
 && claude --version

WORKDIR /app

# Python dependencies (cached layer).
COPY requirements.txt requirements-api.txt ./
RUN pip install -r requirements-api.txt

# Local speech tools the repo treats as present but does not pin in requirements:
# faster-whisper (word-timestamp captions) and piper-tts (offline TTS fallback).
RUN pip install faster-whisper piper-tts

# Remotion dependencies (cached layer) + Chrome Headless Shell for renders.
COPY remotion-composer/package.json remotion-composer/package-lock.json remotion-composer/
RUN cd remotion-composer \
 && npm ci --no-audit --no-fund \
 && npx remotion browser ensure

# Application code.
COPY . .

# Everything the service writes lives on the /data volume (the code tree is read-only for `app`).
ENV OPENMONTAGE_PROJECTS_DIR=/data/projects
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data && chown -R app:app /data \
 && chmod -R a+rX /app
USER app

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# One worker per container on purpose: jobs run in-process; scale out with more containers.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-graceful-shutdown", "30"]
