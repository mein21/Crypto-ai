# Crypto AI autotrade worker — runs on Fly.io fra (Frankfurt).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUTOTRADE_ENABLED=1 \
    PORT=8080

# matplotlib needs a writable cache, even if we don't render charts here.
ENV MPLCONFIGDIR=/tmp/.matplotlib

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY backend /app/backend
COPY public  /app/public

RUN mkdir -p /data /tmp/.matplotlib
ENV AT_DB_PATH=/data/autotrade.db

EXPOSE 8080

# Single uvicorn worker — autotrade scheduler is in-process and must be unique.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
