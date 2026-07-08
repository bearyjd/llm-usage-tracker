"""Tests for backend/recommendations.py — pure logic, no I/O, no mocks."""

from __future__ import annotations

from backend.recommendations import (
    CRITICAL_PCT,
    WARN_PCT,
    _best_alternative,
    _is_percentage_based,
    recommend,
)
from tests.conftest import make_snapshot


class TestRecommendEmptyInput:
    def test_no_snapshots_returns_no_data_message(self):
        recs = recommend([])
        assert len(recs) == 1
        assert recs[0].provider == "all"
        assert recs[0].priority == 99

    def test_only_api_snapshots_returns_no_data_message(self):
        """source='api' snapshots carry no message counts and are excluded."""
        snapshots = [
            make_snapshot("claude", source="api", messages_used=10, messages_limit=100),
        ]
        recs = recommend(snapshots)
        assert len(recs) == 1
        assert recs[0].provider == "all"


class TestRecommendActionByPct:
    def test_pct_0_is_use(self):
        recs = recommend([make_snapshot("groq", messages_used=0, messages_limit=100)])
        assert recs[0].action == "use"

    def test_pct_79_is_use(self):
        recs = recommend([make_snapshot("groq", messages_used=79, messages_limit=100)])
        assert recs[0].action == "use"

    def test_pct_80_is_warn(self):
        recs = recommend([make_snapshot("groq", messages_used=80, messages_limit=100)])
        assert recs[0].action == "warn"

    def test_pct_89_is_warn(self):
        recs = recommend([make_snapshot("groq", messages_used=89, messages_limit=100)])
        assert recs[0].action == "warn"

    def test_pct_90_is_avoid_or_wait(self):
        recs = recommend([make_snapshot("groq", messages_used=90, messages_limit=100)])
        assert recs[0].action in ("avoid", "wait")

    def test_pct_100_is_avoid_or_wait(self):
        recs = recommend([make_snapshot("groq", messages_used=100, messages_limit=100)])
        assert recs[0].action in ("avoid", "wait")

    def test_boundary_values_match_module_thresholds(self):
        """Sanity-check the test assumptions track the module's own constants."""
        assert WARN_PCT == 0.80
        assert CRITICAL_PCT == 0.90


class TestRecommendResetTiming:
    def test_critical_with_reset_soon_is_wait(self):
        snap = make_snapshot(
            "claude", messages_used=95, messages_limit=100, reset_in_minutes=10
        )
        recs = recommend([snap])
        assert recs[0].action == "wait"

    def test_critical_with_reset_far_future_is_avoid(self):
        snap = make_snapshot(
            "claude", messages_used=95, messages_limit=100, reset_in_minutes=180
        )
        recs = recommend([snap])
        assert recs[0].action == "avoid"

    def test_critical_with_no_reset_info_is_avoid(self):
        snap = make_snapshot(
            "claude", messages_used=95, messages_limit=100, reset_in_minutes=None
        )
        recs = recommend([snap])
        assert recs[0].action == "avoid"

    def test_reset_in_the_past_treated_as_now(self):
        """minutes_until_reset() clamps negative deltas to 0.0, so a past reset
        reads as 'resets now' — well within the wait window."""
        snap = make_snapshot(
            "claude", messages_used=95, messages_limit=100, reset_in_minutes=-30
        )
        recs = recommend([snap])
        assert recs[0].action == "wait"

    def test_unavailable_usage_data_is_unknown(self):
        snap = make_snapshot("claude", messages_used=None, messages_limit=None)
        recs = recommend([snap])
        assert recs[0].action == "unknown"
        assert recs[0].priority == 50


class TestRecommendPriorityOrdering:
    def test_lower_priority_number_sorts_first(self):
        critical = make_snapshot("claude", messages_used=95, messages_limit=100)
        healthy = make_snapshot("groq", messages_used=10, messages_limit=100)
        recs = recommend([healthy, critical])
        # 'avoid' (priority=20) must outrank 'use' (priority>=60) regardless of input order.
        assert recs[0].provider == "claude"
        assert recs[0].action == "avoid"
        assert recs[-1].provider == "groq"
        assert recs[-1].action == "use"
        assert recs[0].priority < recs[-1].priority

    def test_results_are_sorted_by_priority_ascending(self):
        snapshots = [
            make_snapshot("claude", messages_used=95, messages_limit=100),
            make_snapshot("groq", messages_used=85, messages_limit=100),
            make_snapshot("gemini", messages_used=10, messages_limit=100),
        ]
        recs = recommend(snapshots)
        priorities = [r.priority for r in recs]
        assert priorities == sorted(priorities)


class TestBestAlternative:
    def test_returns_provider_with_lowest_usage_below_warn_threshold(self):
        snapshots = [
            make_snapshot("claude", messages_used=85, messages_limit=100),
            make_snapshot("groq", messages_used=20, messages_limit=100),
            make_snapshot("gemini", messages_used=50, messages_limit=100),
        ]
        alt = _best_alternative("claude", snapshots)
        assert alt == "groq"

    def test_excludes_the_given_provider(self):
        snapshots = [make_snapshot("claude", messages_used=10, messages_limit=100)]
        alt = _best_alternative("claude", snapshots)
        assert alt is None

    def test_returns_none_when_no_others_have_usage_data(self):
        snapshots = [
            make_snapshot("claude", messages_used=85, messages_limit=100),
            make_snapshot("groq", messages_used=None, messages_limit=None),
        ]
        alt = _best_alternative("claude", snapshots)
        assert alt is None

    def test_returns_none_when_best_alternative_is_also_above_warn_threshold(self):
        snapshots = [
            make_snapshot("claude", messages_used=95, messages_limit=100),
            make_snapshot("groq", messages_used=85, messages_limit=100),
        ]
        alt = _best_alternative("claude", snapshots)
        assert alt is None


class TestIsPercentageBased:
    def test_claude_at_100_limit_is_percentage_based(self):
        snap = make_snapshot("claude", messages_used=50, messages_limit=100)
        assert _is_percentage_based(snap) is True

    def test_claude_with_non_100_limit_is_not_percentage_based(self):
        """Claude's percentage encoding relies on messages_limit == 100 as a proxy;
        anything else means real message counts."""
        snap = make_snapshot("claude", messages_used=50, messages_limit=45)
        assert _is_percentage_based(snap) is False

    def test_other_provider_with_100_limit_is_not_percentage_based(self):
        """Footgun case: a non-Claude provider that happens to have a 100-message
        limit must NOT be misread as percentage-based."""
        snap = make_snapshot("groq", messages_used=50, messages_limit=100)
        assert _is_percentage_based(snap) is False

    def test_non_claude_provider_never_percentage_based(self):
        snap = make_snapshot("gemini", messages_used=10, messages_limit=20)
        assert _is_percentage_based(snap) is False
