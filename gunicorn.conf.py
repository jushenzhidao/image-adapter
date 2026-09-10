"""Gunicorn configuration tuned for the adapter's workload.

The adapter is IO-bound in an extreme way: a single 2K image request blocks on
one upstream call for 27~82s (measured against Ark), and the process does no
CPU work of its own beyond JSON/base64 handling. That shape dictates every
choice below.

Worker model
    UvicornWorker (asyncio), not sync workers. A sync worker serves exactly one
    request at a time, so a handful of 80s generations would exhaust the pool
    and queue everything behind them. Each async worker instead multiplexes
    thousands of in-flight upstream waits on one event loop.

Concurrency
    Async workers are not CPU-parallel, so worker count tracks cores only to
    use every core for the (small) serialization cost, while real concurrency
    comes from the loop. WEB_CONCURRENCY overrides it; that env var is the de
    facto standard and lets one image serve a 2-core and a 32-core host.

Timeouts
    `timeout` is a worker liveness watchdog, not a request budget. It must stay
    above UPSTREAM_TIMEOUT (180s in the shipped .env) or gunicorn will SIGKILL
    a worker that is legitimately waiting on a slow generation. Request budgets
    belong to the aiohttp client, which enforces them per upstream call.
"""

from __future__ import annotations

import multiprocessing
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


bind = f"0.0.0.0:{_int_env('PORT', 8080)}"

# One worker per core by default. Async workers get their concurrency from the
# event loop, so this is about spreading serialization across cores rather than
# about how many requests can be in flight.
workers = _int_env("WEB_CONCURRENCY", multiprocessing.cpu_count())
worker_class = "uvicorn.workers.UvicornWorker"

# Per-worker cap on simultaneous connections. 1000 x N workers is far beyond
# what upstreams tolerate, but the ceiling exists to shed load rather than to
# be reached: aiohttp's own pool (HTTP_POOL_LIMIT) is the real throttle.
worker_connections = _int_env("WORKER_CONNECTIONS", 1000)

# Liveness watchdog, deliberately larger than UPSTREAM_TIMEOUT. A 2K generation
# holds the coroutine for well over a minute; killing the worker mid-wait would
# turn a slow success into a 502.
timeout = _int_env("GUNICORN_TIMEOUT", 300)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 120)

# Above any sane reverse-proxy idle timeout so the proxy, not gunicorn, decides
# when an idle keep-alive connection ends.
keepalive = _int_env("GUNICORN_KEEPALIVE", 65)

# Recycle workers to bound the effect of any slow leak in a long-lived process.
# The jitter stops all workers from retiring on the same request.
max_requests = _int_env("GUNICORN_MAX_REQUESTS", 10000)
max_requests_jitter = _int_env("GUNICORN_MAX_REQUESTS_JITTER", 1000)

# The container root filesystem is read-only; the default /tmp heartbeat file
# would fail, so point it at the tmpfs mount.
worker_tmp_dir = os.environ.get("GUNICORN_WORKER_TMP_DIR", "/tmp")

# Accept a burst without refusing connections while workers are busy.
backlog = _int_env("GUNICORN_BACKLOG", 2048)

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Request ids and upstream latency already flow through Logfire, so the access
# log only needs enough to correlate: method, path, status, duration.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

# Preloading would share one import of the app across workers, but it also
# shares any object created at import time. logfire.configure() and the aiohttp
# session are per-process resources, so each worker builds its own.
preload_app = False
