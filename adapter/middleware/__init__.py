"""Middleware package: request logging, rate limit, body limit, CORS.

Everything here is raw ASGI rather than Starlette's ``BaseHTTPMiddleware``,
which measures roughly +0.4 ms per request -- about twenty times the cost of
the framework layer it wraps.
"""
