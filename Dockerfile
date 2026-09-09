FROM python:3.13-slim

# Security: run as non-root user
RUN groupadd -r adapter && useradd -r -g adapter adapter

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY adapter /app/adapter
COPY scripts /app/scripts
COPY config /app/config
COPY mock_upstream /app/mock_upstream

# Non-root ownership
RUN chown -R adapter:adapter /app

# Read-only root filesystem (tmpfs for /tmp mounted in compose)
USER adapter

EXPOSE 8080

CMD ["gunicorn", "adapter.main:app", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8080", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
