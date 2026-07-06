FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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
RUN grep -vE '^MolScribe([[:space:]=<>].*)?$' /tmp/requirements.txt > /tmp/requirements-base.txt \
    && grep -vE '^torch([[:space:]=<>].*)?$' /tmp/requirements-base.txt > /tmp/requirements-no-torch.txt \
    && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu121 \
        torch==2.5.1+cu121 \
        torchvision==0.20.1+cu121 \
    && pip install --no-cache-dir -r /tmp/requirements-no-torch.txt \
    && pip install --no-cache-dir \
        'numpy<2' \
        albumentations==1.1.0 \
        opencv-python-headless==4.10.0.84 \
    && pip install --no-cache-dir --no-deps \
        MolScribe==1.1.1 \
        OpenNMT-py==2.2.0 \
        SmilesPE==0.0.3 \
        timm==0.4.12

COPY backend /app/backend
COPY model /app/model

WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
