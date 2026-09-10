FROM python:3.13-slim

# Security: run as non-root. The uid is fixed at 1000 so it matches the
# ownership of any host directory a deployment chooses to mount, and so the
# container needs no `user:` override to stay non-root.
RUN groupadd -g 1000 adapter && useradd -u 1000 -g 1000 -m adapter

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code. script_store ships inside the image, so SCRIPT_REF_DIR
# resolves named refs (vendor_y/mj@v1.3) with no bind mount and the deployment
# has no host-path dependency. mock_upstream is a test stub and stays out.
#
# A deployment that wants to ship a script without rebuilding can mount extra
# read-only roots and list them in SCRIPT_OVERLAY_DIRS: those are searched
# first, and this baked store remains the always-present fallback.
COPY adapter /app/adapter
COPY script_store /app/script_store
COPY gunicorn.conf.py /app/gunicorn.conf.py

# Non-root ownership
RUN chown -R adapter:adapter /app

# Read-only root filesystem (tmpfs for /tmp mounted in compose)
USER adapter

EXPOSE 8080

# Tuning lives in gunicorn.conf.py and is env-overridable (WEB_CONCURRENCY,
# GUNICORN_TIMEOUT, ...), so one image fits a 2-core box and a 32-core box.
CMD ["gunicorn", "adapter.main:app", "--config", "/app/gunicorn.conf.py"]
