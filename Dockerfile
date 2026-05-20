FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQLITE_DB_PATH=/app/backend/data/polyprop.db \
    FUMOL_DB_PATH=/app/backend/data/fumol.db \
    MODEL_DIR=/app/model \
    MODEL_ENABLED=true \
    GEN_MODEL_ENABLED=false \
    GEN_MODEL_DIR=/app/model/conditional_generation \
    GEN_DEVICE=auto \
    GEN_JOB_WORKERS=1 \
    ALLOWED_ORIGINS=http://localhost:9000,http://127.0.0.1:9000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY backend /app/backend
COPY model /app/model

WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
