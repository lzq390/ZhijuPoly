FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MONOMER_MD_WORKER_HOST=0.0.0.0 \
    MONOMER_MD_WORKER_PORT=8010

WORKDIR /app

COPY workers/monomer_md_worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY workers/monomer_md_worker /app/workers/monomer_md_worker

WORKDIR /app/workers/monomer_md_worker
EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
