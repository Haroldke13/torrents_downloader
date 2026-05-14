FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000 \
    DOWNLOADS_DIR=/app/downloads \
    MAX_CONCURRENT_DOWNLOADS=1 \
    DOWNLOAD_POLL_INTERVAL=1.0 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=120

WORKDIR /app

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/downloads \
    && chmod +x /app/start.sh

EXPOSE 10000

CMD ["./start.sh"]
