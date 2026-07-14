FROM pytorch/pytorch:2.6.0-cuda11.8-cudnn9-runtime@sha256:2428b92ebbaeceba5572b98c18c8a94e43162bead6e88588ad54471147c58a20

ARG PYPI_INDEX_URL="https://pypi.org/simple"
ARG PYPI_MIRROR_URL="https://pypi.org/simple"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_CONCURRENCY=1 \
    MODEL_DIR=/app/model \
    MODEL_ENABLED=true \
    OCSR_ENABLED=true \
    OCSR_MODEL_DIR=/app/model/ocsr \
    OCSR_DEVICE=auto \
    OCSR_MAX_IMAGE_BYTES=5242880 \
    GEN_MODEL_ENABLED=false \
    GEN_MODEL_DIR=/app/model/conditional_generation \
    GEN_DEVICE=auto \
    GEN_JOB_WORKERS=1 \
    GEN_MAX_ACTIVE_JOBS=8 \
    GPU_PRELOAD_MODE=lazy \
    GPU_MAX_CONCURRENT_INFERENCES=1 \
    GPU_MAX_WAITING_INFERENCES=8 \
    GPU_SYNC_QUEUE_TIMEOUT_SECONDS=30 \
    GPU_ASYNC_QUEUE_TIMEOUT_SECONDS=600 \
    POLYTAO_ENABLED=false \
    POLYTAO_MODEL_DIR=/app/model/polytao \
    POLYTAO_DEVICE=auto \
    POLYTAO_JOB_THREADS=1 \
    ALLOWED_ORIGINS=http://localhost:9000,http://127.0.0.1:9000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgomp1 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.lock /tmp/requirements.lock
COPY backend/requirements-system.lock /tmp/requirements-system.lock
COPY backend/requirements-legacy.lock /tmp/requirements-legacy.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python --version | grep -Eq '^Python 3\.11\.' \
    && python -m pip install --no-index --no-deps --require-hashes \
        -r /tmp/requirements-system.lock \
    && python -c "import importlib.metadata as m, torch; expected={'torch':'2.6.0+cu118','torchvision':'0.21.0+cu118'}; actual={name:m.version(name) for name in expected}; assert actual == expected, (actual, expected); assert torch.version.cuda == '11.8', torch.version.cuda" \
    && python -m pip install --only-binary=:all: --require-hashes \
        --retries 10 \
        --timeout 120 \
        --index-url "$PYPI_INDEX_URL" \
        --extra-index-url "$PYPI_MIRROR_URL" \
        -r /tmp/requirements.lock \
    && python -m pip install --no-deps --require-hashes \
        --retries 10 \
        --timeout 120 \
        -r /tmp/requirements-legacy.lock \
    && python -c "import importlib.metadata as m, torch; expected={'torch':'2.6.0+cu118','torchvision':'0.21.0+cu118','transformers':'4.57.6','scikit-learn':'1.8.0'}; actual={name:m.version(name) for name in expected}; assert actual == expected, (actual, expected); assert torch.version.cuda == '11.8', torch.version.cuda"

COPY backend /app/backend
RUN mkdir -p /app/model

ARG SOURCE_REVISION="unknown"
ARG SOURCE_URL="https://github.com/lzq390/ZhijuPoly"
ARG VERSION="dev"

LABEL org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.version="$VERSION"

ENV BUILD_REVISION=${SOURCE_REVISION}

WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "40"]
