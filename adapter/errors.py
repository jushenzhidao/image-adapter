"""Unified error model.

Client-facing errors use the OpenAI error envelope and never leak stack
traces or internal orchestration details. Model/channel resolution errors do
not exist here: the control plane resolved those before calling us.
"""

from __future__ import annotations

from adapter.jsoncodec import JSONResponse


class AdapterError(Exception):
    """Base error carrying HTTP status plus OpenAI-style error fields."""

    def __init__(
        self,
        status: int,
        message: str,
        err_type: str = "server_error",
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type
        self.param = param
        self.code = code

    def to_body(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": self.param,
                "code": self.code,
            }
        }


class InvalidRequestError(AdapterError):
    def __init__(
        self, message: str, param: str | None = None, code: str = "invalid_request"
    ) -> None:
        super().__init__(400, message, "invalid_request_error", param, code)


class PayloadTooLargeError(AdapterError):
    """The request body is over the configured ceiling.

    A distinct code rather than a generic invalid_request, because a control
    plane should be able to tell "this request can never fit" apart from "this
    request is malformed" and stop retrying. 413 also matches what a reverse
    proxy returns for the same condition, so the client sees one shape either
    way -- except that this one carries the OpenAI envelope.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            413,
            f"Request body exceeds the {limit} byte limit",
            "invalid_request_error",
            None,
            "request_too_large",
        )


class AdmissionError(AdapterError):
    """X-Adapter-Key missing or wrong. Distinct from upstream credentials."""

    def __init__(self, message: str = "Missing or invalid X-Adapter-Key") -> None:
        super().__init__(401, message, "authentication_error", None, "invalid_adapter_key")


class ChannelConfigError(AdapterError):
    """The channel headers supplied by the control plane are unusable."""

    def __init__(self, message: str, header: str | None = None) -> None:
        super().__init__(
            400, message, "invalid_request_error", header, "channel_config_error"
        )


class ScriptSourceError(AdapterError):
    """Script could not be located, fetched, or verified."""

    def __init__(self, message: str, code: str = "script_source_error") -> None:
        super().__init__(400, message, "invalid_request_error", None, code)


class ScriptPolicyError(AdapterError):
    """Script source is disabled by deployment policy."""

    def __init__(self, message: str) -> None:
        super().__init__(403, message, "invalid_request_error", None, "script_forbidden")


class SecurityError(AdapterError):
    """AST sandbox rejected the script."""

    def __init__(self, message: str) -> None:
        super().__init__(
            400, message, "invalid_request_error", None, "script_security_error"
        )


class ScriptRuntimeError(AdapterError):
    """Script raised during execution. Original traceback is not echoed back."""

    def __init__(self, phase: str, detail: str = "") -> None:
        message = f"Script failed during phase '{phase}'"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(500, message, "server_error", None, "script_runtime_error")
        self.phase = phase


class ScriptTimeoutError(AdapterError):
    def __init__(self, phase: str, timeout: float) -> None:
        super().__init__(
            504,
            f"Script execution timed out after {timeout:.0f}s in phase '{phase}'",
            "server_error",
            None,
            "script_timeout",
        )
        self.phase = phase


class UpstreamError(AdapterError):
    def __init__(
        self,
        message: str = "Upstream request failed",
        code: str = "upstream_error",
        status: int = 502,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(status, message, "upstream_error", None, code)
        self.upstream_status = upstream_status


class PollTimeoutError(AdapterError):
    def __init__(self, timeout: float) -> None:
        super().__init__(
            504,
            f"Upstream job did not finish within {timeout:.0f}s",
            "server_error",
            None,
            "poll_timeout",
        )


class PipelineTimeoutError(AdapterError):
    """The shared cascade budget ran out (AC-27).

    Distinct from StageTimeoutError: this means the whole request is over
    budget, so retrying the stage would not help.
    """

    def __init__(
        self, budget: float, stage: str | None = None, during: bool = False
    ) -> None:
        where = ""
        if stage:
            # "during" is the call that ran out the clock; "before" is a stage
            # that never started because the clock was already spent.
            where = f" {'during' if during else 'before'} stage '{stage}'"
        super().__init__(
            504,
            f"Pipeline exceeded its {budget:.0f}s time budget{where}",
            "server_error",
            None,
            "pipeline_budget_exceeded",
        )
        self.stage = stage


class StageTimeoutError(AdapterError):
    """One stage's upstream call exceeded the per-stage cap."""

    def __init__(self, stage: str, timeout: float) -> None:
        super().__init__(
            504,
            f"Stage '{stage}' did not finish within {timeout:.0f}s",
            "server_error",
            None,
            "stage_timeout",
        )
        self.stage = stage


class RateLimitError(AdapterError):
    def __init__(self, message: str = "Rate limit exceeded, retry later") -> None:
        super().__init__(429, message, "rate_limit_error", None, "rate_limit_exceeded")


def error_response(exc: AdapterError, request_id: str | None = None) -> JSONResponse:
    headers = {"X-Request-Id": request_id} if request_id else None
    return JSONResponse(exc.to_body(), status_code=exc.status, headers=headers)


def internal_error_response(request_id: str | None = None) -> JSONResponse:
    exc = AdapterError(500, "Internal server error", "server_error", None, "internal_error")
    return error_response(exc, request_id)
