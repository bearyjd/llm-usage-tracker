"""Fixture-driven tests for ClaudeCollector._parse_usage_response.

These exercise only the pure parsing branch — no Playwright, no network,
no live session/credentials. Fixtures are synthetic, representative-shape
JSON, not captured from any real account.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.collectors.claude import ClaudeCollector

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _parse(fixture_name: str):
    collector = ClaudeCollector()
    snapshot = collector._base_snapshot()
    data = _load_fixture(fixture_name)
    return collector._parse_usage_response(snapshot, data)


class TestParseUsageResponseBothWindows:
    """Both five_hour and seven_day utilization present; the more restrictive
    (higher-utilization) window should win."""

    def test_picks_the_more_restrictive_window(self):
        snapshot = _parse("claude_usage_response.json")
        # seven_day (62%) is more restrictive than five_hour (45%)
        assert snapshot.messages_used == 62
        assert snapshot.messages_window_hours == 168.0

    def test_messages_limit_is_100_for_percentage_based_windows(self):
        snapshot = _parse("claude_usage_response.json")
        assert snapshot.messages_limit == 100

    def test_reset_timestamp_is_parsed(self):
        snapshot = _parse("claude_usage_response.json")
        assert snapshot.messages_reset_at is not None
        assert snapshot.messages_reset_at.year == 2026
        assert snapshot.messages_reset_at.month == 7
        assert snapshot.messages_reset_at.day == 10  # seven_day window's reset

    def test_model_tier_from_plan_name(self):
        snapshot = _parse("claude_usage_response.json")
        assert snapshot.model_tier == "Max"

    def test_features_capture_extra_usage_and_per_model_windows(self):
        snapshot = _parse("claude_usage_response.json")
        assert "extra_usage" in snapshot.features
        assert "seven_day_opus" in snapshot.features
        assert "seven_day_sonnet" in snapshot.features


class TestParseUsageResponseMessageCounts:
    """messages_sent/messages_limit shape (as opposed to a raw utilization pct)."""

    def test_computes_percentage_from_counts(self):
        snapshot = _parse("claude_usage_response_no_reset.json")
        # 42 / 60 = 70%
        assert snapshot.messages_used == 70
        assert snapshot.messages_limit == 100
        assert snapshot.messages_window_hours == 5.0

    def test_no_reset_timestamp_present_leaves_it_none(self):
        snapshot = _parse("claude_usage_response_no_reset.json")
        assert snapshot.messages_reset_at is None


class TestParseUsageResponseMissingFields:
    def test_no_utilization_windows_leaves_messages_unset(self):
        snapshot = _parse("claude_usage_response_empty.json")
        assert snapshot.messages_used is None
        assert snapshot.messages_limit is None

    def test_plan_name_still_captured(self):
        snapshot = _parse("claude_usage_response_empty.json")
        assert snapshot.model_tier == "Free"

    def test_no_features_recorded_when_no_windows_present(self):
        snapshot = _parse("claude_usage_response_empty.json")
        assert snapshot.features == {}
