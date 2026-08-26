# ── Stage 1: install dependencies ────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv so we can copy it cleanly to the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user for security
RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application code (owned by app user)
COPY --chown=app:app . .

# Create writable instance directory for SQLite checkpointing
RUN mkdir -p /app/instance && chown app:app /app/instance

USER app

EXPOSE 8080

# Config in gunicorn.conf.py — override WEB_WORKERS / WEB_THREADS via env
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
