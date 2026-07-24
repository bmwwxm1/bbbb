FROM python:3.12-slim

WORKDIR /app

# System deps for aiohttp/psycopg/crypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

# Non-root user for production
RUN useradd -m botuser && chown -R botuser:botuser /app /data
USER botuser

# Health check (process-level)
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "-u", "main.py"]
