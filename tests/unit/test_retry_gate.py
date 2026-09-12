"""Unit tests for the gate in front of the retry: is this failure even offered?

The engine answers every upstream *refusal* by handing the failure to the script
once more. What counts as a refusal is the whole question here, and the answer
is narrower than "the call failed": only the upstream telling us *our request was
refused* is worth answering with a different second attempt. Everything else
leaves the outcome unknown, and a second send on an unknown outcome is how one
request gets billed twice -- so those cases are pinned as hard noes.
"""

from __future__ import annotations

import pytest

from adapter.executor import _request_was_refused


class TestWhatCountsAsARefusal:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
    def test_a_4xx_is_a_statement_about_our_request(self, status):
        """Including 429: the upstream did answer, and it answered about this
        request. Whether a rate limit is worth a second attempt is the
        *script's* judgment, not the engine's."""
        assert _request_was_refused(status)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_5xx_leaves_the_outcome_unknown(self, status):
        """A gateway error does not say the generation never started. Re-sending
        on that reading is exactly the double-billing the project refuses."""
        assert not _request_was_refused(status)

    def test_an_error_without_a_vendor_status_is_not_a_refusal(self):
        """Three of `_do_upstream`'s four failure modes carry no status at all:
        `upstream_timeout`, `upstream_unreachable` and `upstream_body_too_large`.
        None of them says the request was refused, so none is offered again."""
        assert not _request_was_refused(None)

    @pytest.mark.parametrize("status", [200, 302, 399], ids=["ok", "redirect", "near-4xx"])
    def test_a_non_failure_is_not_a_refusal(self, status):
        """`raise_for_status` only raises for >= 400; the predicate still has to
        be right on its own, because it is what a reader checks first."""
        assert not _request_was_refused(status)

    def test_the_boundary_is_exclusive_on_the_top(self):
        assert _request_was_refused(499)
        assert not _request_was_refused(500)
