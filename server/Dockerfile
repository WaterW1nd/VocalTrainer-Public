# ──────────────────────────────────────────────────────────────────────────────
# Karaoke Vocal Trainer — tools_server Docker image
# Base: python:3.11-slim (Debian bookworm)
# Tools: ffmpeg (system), demucs 4.0.1 (CPU), basic-pitch 0.4.0 (TFLite)
# Port: 5050
#
# ПЕРЕД СБОРКОЙ: положи 4 файла моделей в папку models/ рядом с Dockerfile:
#   models/f7e0c4bc-ba3fe64a.th
#   models/d12395a8-e57c48e6.th
#   models/92cfc3b6-ef3bcb9c.th
#   models/04573f0d-f3cf25b2.th
# Скачать: см. TOOLS_GUIDE.md раздел «Загрузка моделей»
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

ENV PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git curl libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── torch CPU-only ────────────────────────────────────────────────────────────
RUN curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
        "https://download.pytorch.org/whl/cpu/torch-2.3.1%2Bcpu-cp311-cp311-linux_x86_64.whl" \
        -o "torch-2.3.1+cpu-cp311-cp311-linux_x86_64.whl" \
    && pip install --no-deps --no-cache-dir "torch-2.3.1+cpu-cp311-cp311-linux_x86_64.whl" \
    && rm "torch-2.3.1+cpu-cp311-cp311-linux_x86_64.whl"

RUN pip install --no-cache-dir filelock typing_extensions sympy networkx jinja2 fsspec

RUN curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
        "https://download.pytorch.org/whl/cpu/torchaudio-2.3.1%2Bcpu-cp311-cp311-linux_x86_64.whl" \
        -o "torchaudio-2.3.1+cpu-cp311-cp311-linux_x86_64.whl" \
    && pip install --no-deps --no-cache-dir "torchaudio-2.3.1+cpu-cp311-cp311-linux_x86_64.whl" \
    && rm "torchaudio-2.3.1+cpu-cp311-cp311-linux_x86_64.whl"

# ── Demucs + Basic Pitch ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 300 --retries 10 -r requirements.txt

# ── Модели Demucs (скопированы локально) ─────────────────────────────────────
# Кэш torch.hub по умолчанию: $TORCH_HOME/hub/checkpoints/
ENV TORCH_HOME=/models
COPY models/ /models/hub/checkpoints/

# ── Код сервера ───────────────────────────────────────────────────────────────
COPY tools_server.py .

ENV PORT=5050 \
    LISTEN_HOST=0.0.0.0 \
    DEMUCS_MODEL=htdemucs_ft \
    FFMPEG_CMD=ffmpeg \
    CONVERT_SAMPLE_RATE=44100 \
    CONVERT_CHANNELS=2 \
    ALLOW_ORIGIN=* \
    MAX_JOB_AGE_SEC=86400 \
    TORCH_HOME=/models

RUN mkdir -p /app/tmp

EXPOSE 5050

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5050/api/ping')" \
    || exit 1

CMD ["python", "tools_server.py"]
