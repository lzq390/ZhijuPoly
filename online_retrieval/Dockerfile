FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    HISTORY_FILE=/app/online_retrieval/data/search_history.json

WORKDIR /app/online_retrieval

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY online_retrieval/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY online_retrieval/app.py /app/online_retrieval/app.py
COPY online_retrieval/static /app/online_retrieval/static
RUN mkdir -p /app/online_retrieval/data /tmp/matplotlib

EXPOSE 5002

CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "900", "--bind", "0.0.0.0:5002", "app:app"]
