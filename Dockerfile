FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:77f17f843507062875ce8be2a6f76aa6aa3df7f9ef1e31d9d7432f4b0f563dee AS backend-base

ARG PYPI_INDEX_URL="https://pypi.org/simple"
ARG PYPI_MIRROR_URL="https://pypi.org/simple"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/nexpoly \
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
    GPU_BROKER_ENABLED=false \
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
    && python -c "import importlib.metadata as m, torch; expected={'torch':'2.6.0+cu124','torchvision':'0.21.0+cu124'}; actual={name:m.version(name) for name in expected}; assert actual == expected, (actual, expected); assert torch.version.cuda == '12.4', torch.version.cuda" \
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
    && python -c "import importlib.metadata as m, torch; expected={'torch':'2.6.0+cu124','torchvision':'0.21.0+cu124','transformers':'4.57.6','scikit-learn':'1.8.0'}; actual={name:m.version(name) for name in expected}; assert actual == expected, (actual, expected); assert torch.version.cuda == '12.4', torch.version.cuda"

COPY backend /app/backend
COPY gpu_resource /app/gpu_resource
RUN mkdir -p /app/model \
    && (getent group 1001 >/dev/null || groupadd --gid 1001 nexpoly) \
    && (getent passwd 1001 >/dev/null || useradd --uid 1001 --gid 1001 --create-home nexpoly) \
    && install -d -o 1001 -g 1001 -m 0700 /home/nexpoly \
    && chown -R 1001:1001 /app

ARG SOURCE_REVISION="unknown"
ARG SOURCE_TREE="unknown"
ARG DEPENDENCY_LOCK_SHA256="unknown"
ARG BUILD_CONFIG_SHA256="unknown"
ARG SOURCE_URL="https://github.com/lzq390/ZhijuPoly"
ARG VERSION="dev"

LABEL org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      com.nexpoly.source.tree="$SOURCE_TREE" \
      com.nexpoly.backend.dependency-lock="$DEPENDENCY_LOCK_SHA256" \
      com.nexpoly.backend.build-config="$BUILD_CONFIG_SHA256" \
      org.opencontainers.image.version="$VERSION"

ENV BUILD_REVISION=${SOURCE_REVISION} \
    BUILD_SOURCE_TREE=${SOURCE_TREE} \
    BUILD_DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256} \
    BUILD_CONFIG_SHA256=${BUILD_CONFIG_SHA256}

WORKDIR /app/backend
ENV PYTHONPATH=/app
USER 1001:1001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "40"]

# Development and CI tests deliberately use a separate image target.  The
# runtime image remains free of pytest while the test target is still built
# from the exact same locked runtime and source tree.
FROM backend-base AS backend-test

USER root
COPY backend/requirements-ci.lock /tmp/requirements-ci.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --only-binary=:all: --require-hashes \
        --retries 10 \
        --timeout 120 \
        --index-url "$PYPI_INDEX_URL" \
        --extra-index-url "$PYPI_MIRROR_URL" \
        -r /tmp/requirements-ci.lock
USER 1001:1001

CMD ["python", "-m", "pytest", "tests"]

FROM backend-base AS runtime
