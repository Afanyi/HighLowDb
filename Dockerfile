FROM python:3.13.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/trading212_stocks.db \
    CRYPTO_DB_PATH=/data/crypto.db

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user; /data is created here so the named volume inherits its ownership.
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app:app /data

COPY sync.py app.py crypto_sync.py ./
COPY templates/ templates/
COPY tests/ tests/

USER app
EXPOSE 8000

# One worker = exactly one scheduler thread. Do not add --preload or more workers.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "app:app"]
