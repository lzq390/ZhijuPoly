FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y libxrender1 libxext6 && \
    rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

WORKDIR /app/backend
RUN python -m app.import_csv

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
