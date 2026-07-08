FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POLYTAO_WORKER_HOST=0.0.0.0 \
    POLYTAO_WORKER_PORT=8020

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY workers/polytao_worker/requirements.txt /tmp/requirements.txt
RUN grep -vE '^torch([[:space:]=<>].*)?$' /tmp/requirements.txt > /tmp/requirements-no-torch.txt \
    && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu118 \
        torch==2.6.0+cu118 \
    && pip install --no-cache-dir -r /tmp/requirements-no-torch.txt

COPY workers/polytao_worker /app/workers/polytao_worker

WORKDIR /app/workers/polytao_worker
EXPOSE 8020

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]
