FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mywhoosh_workouts_unique.csv .
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    PORT=8555

EXPOSE 8555

VOLUME ["/app/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8555"]
