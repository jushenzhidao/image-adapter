"""``ctx.http`` when the channel declared a proxy: one session, proxy per call.

aiohttp takes a proxy per *request*, not per session, so a channel-level proxy
cannot be expressed by handing a script a different session object: the script
calls ``ctx.http.get(url)`` and would simply go direct. This view is the shared
session wrapped so that every call whose target is in scope carries ``proxy``
-- and, for a rotating channel, the request's session id as the proxy login.

Two properties matter more than the mechanism:

* it is **not** a second connection pool. The wrapped session is the same one
  the engine uses (the process-wide session, or the request-scoped one a
  rotating channel gets), so keep-alive and the pool limits are unaffected.
  That is also what makes "one outbound tunnel per request" true: the engine's
  generation call and the script's own calls share one pool and therefore one
  socket to the bridge.
* ``request`` is handled separately from the verbs, because its URL is the
  *second* positional argument while ``get``/``post``/``put`` take it first.
  Getting that wrong would silently skip the proxy on exactly the calls that
  need it, and nothing would look broken.

Anything not proxied -- the material download path, for one -- does not go
through this view at all; see ``AdapterContext.download_http``.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from adapter.proxyplan import ProxyPlan

#: Verbs whose first positional argument is the URL. `request` is excluded on
#: purpose and defined explicitly below.
_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")


class ProxiedHttp:
    """A view of a session that attaches the channel's proxy per call."""

    def __init__(self, session: aiohttp.ClientSession, plan: ProxyPlan) -> None:
        self._session = session
        self._plan = plan

    def __repr__(self) -> str:
        return f"ProxiedHttp({self._plan.url!r} hosts={self._plan.hosts!r})"

    # -- proxied verbs ------------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._session.request(method, url, **self._scoped(url, kwargs))

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._session.get(url, **self._scoped(url, kwargs))

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._session.post(url, **self._scoped(url, kwargs))

    def put(self, url: str, **kwargs: Any) -> Any:
        return self._session.put(url, **self._scoped(url, kwargs))

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self._session.patch(url, **self._scoped(url, kwargs))

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self._session.delete(url, **self._scoped(url, kwargs))

    def head(self, url: str, **kwargs: Any) -> Any:
        return self._session.head(url, **self._scoped(url, kwargs))

    def options(self, url: str, **kwargs: Any) -> Any:
        return self._session.options(url, **self._scoped(url, kwargs))

    # -- everything else passes straight through -----------------------------

    def __getattr__(self, name: str) -> Any:
        """`closed`, `cookie_jar`, `connector`, `close` -- the rest of the session.

        Only reached for attributes this class does not define, so the verbs
        above keep their proxy and nothing else changes shape.
        """
        return getattr(self._session, name)

    # -- internals ----------------------------------------------------------

    def _scoped(self, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if not self._plan.covers(url):
            return kwargs
        kwargs.setdefault("proxy", self._plan.url)
        credentials = self._plan.credentials()
        if credentials is not None:
            # The same header the engine sends, so both land on one tunnel. A
            # header rather than `proxy_auth`, which aiohttp has deprecated.
            kwargs.setdefault(
                "proxy_headers",
                {"Proxy-Authorization": aiohttp.encode_basic_auth(*credentials)},
            )
        return kwargs
