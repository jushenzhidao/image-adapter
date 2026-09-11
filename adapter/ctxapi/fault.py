"""A typed failure path for scripts.

An upstream can answer 200 and still fail the task. An image model refuses on
safety grounds, returns prose instead of pixels, or reports "no image" outright
-- and none of that is visible to the engine, so the script has to say so. Until
this mixin existed it had no vocabulary for it: raising a bare exception becomes
``ScriptRuntimeError`` (500), and returning ``{"error": ...}`` is written out as
a 200 body. Both spell "the adapter is broken" for something that is really a
client-visible refusal, and a control plane cannot tell the two apart.

``AdapterError`` is the one exception ``executor._call_phase`` re-raises
untouched, which is what makes a one-line helper enough.

Scripts see ``ctx.fail(...)``; they never import the error classes themselves
(``sandbox.ALLOWED_STDLIB`` deliberately excludes adapter internals).
"""

from __future__ import annotations

from typing import NoReturn

from adapter.ctxapi.base import CtxMixin
from adapter.errors import AdapterError


class FaultMixin(CtxMixin):
    """Lets a script end the request with a client-facing error."""

    def fail(
        self,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        status: int = 400,
        err_type: str = "invalid_request_error",
    ) -> NoReturn:
        """Raises a client-visible error instead of a 500 or a silent 200.

        The defaults describe the common case -- the upstream could not produce
        what the client asked for -- so a script writes
        ``ctx.fail("...", code="content_filter", param="prompt")`` and the
        client gets a 400 in the OpenAI envelope. Pass ``status=502`` when the
        fault is genuinely upstream's, which is the difference between "do not
        retry" and "retry later" for whoever is downstream.
        """
        raise AdapterError(status, message, err_type, param, code)
