"""Middleware package: request logging, rate limit, CORS.

Everything here is raw ASGI rather than Starlette's ``BaseHTTPMiddleware``,
which measures roughly +0.4 ms per request -- about twenty times the cost of
the framework layer it wraps.

Admission (``X-Adapter-Key``) is deliberately *not* middleware: it runs inside
the pipeline so its rejection carries the adapter's own error code
(``invalid_adapter_key``) through the same OpenAI envelope as every other
failure.
"""
