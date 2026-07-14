FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3

ARG SOURCE_REVISION="unknown"
ARG SOURCE_URL="https://github.com/lzq390/ZhijuPoly"
ARG VERSION="dev"

LABEL org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.version="$VERSION"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MONOMER_MD_WORKER_HOST=0.0.0.0 \
    MONOMER_MD_WORKER_PORT=8010

WORKDIR /app

COPY workers/monomer_md_worker/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes \
        -r /tmp/requirements.lock

COPY workers/monomer_md_worker /app/workers/monomer_md_worker

WORKDIR /app/workers/monomer_md_worker
EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
