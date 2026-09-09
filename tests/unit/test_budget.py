"""Unit tests for the shared cascade budget (BR-011, AC-22 / AC-27)."""

from __future__ import annotations

import time

from adapter.budget import Budget, resolve_total


def test_remaining_counts_down_from_total():
    budget = Budget(total=10.0)
    assert 9.9 < budget.remaining <= 10.0
    assert not budget.exhausted


def test_remaining_floors_at_zero_rather_than_going_negative():
    # Callers pass remaining straight into a timeout, where a negative would
    # mean "no limit" to some clients.
    budget = Budget(total=1.0, started=time.monotonic() - 5.0)
    assert budget.remaining == 0.0
    assert budget.exhausted


def test_cap_returns_the_smaller_of_call_timeout_and_remaining():
    budget = Budget(total=100.0)
    # Plenty of budget left: the call's own timeout governs.
    assert budget.cap(30.0) == 30.0


def test_cap_clamps_when_budget_is_nearly_spent():
    # This is the property that stops N stages each spending a full timeout.
    budget = Budget(total=10.0, started=time.monotonic() - 8.0)
    capped = budget.cap(60.0)
    assert 1.9 < capped <= 2.0


def test_stage_timeouts_cannot_sum_past_the_total():
    budget = Budget(total=5.0, started=time.monotonic() - 4.5)
    first = budget.cap(120.0)
    second = budget.cap(120.0)
    assert first + second <= 1.1  # both draw from the same remainder


def test_elapsed_tracks_wall_clock():
    budget = Budget(total=10.0, started=time.monotonic() - 3.0)
    assert 2.9 < budget.elapsed < 3.2


class TestResolveTotal:
    def test_absent_request_uses_the_default(self):
        assert resolve_total(None, 300.0, 600.0) == 300.0

    def test_caller_value_is_honoured_below_the_ceiling(self):
        assert resolve_total(120.0, 300.0, 600.0) == 120.0

    def test_caller_value_is_capped_at_the_ceiling(self):
        # X-Stage-Timeout is attacker-reachable; an unbounded budget would pin
        # a worker for as long as the caller likes.
        assert resolve_total(99_999.0, 300.0, 600.0) == 600.0

    def test_nonpositive_falls_back_to_the_default(self):
        assert resolve_total(0.0, 300.0, 600.0) == 300.0
        assert resolve_total(-10.0, 300.0, 600.0) == 300.0
