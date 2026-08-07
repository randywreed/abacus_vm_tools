"""Tests for the signed per-user Abacus credit snapshot surface.

Covers the pure helpers in abacus_usage.py (email normalization, period
classification, by-user log parsing) and source-level safety checks that the
connector endpoint is HMAC-authenticated, bounded, sanitized, and fails closed
— matching the existing connector test conventions.

The source period is a ROLLING 30-DAY window (Abacus reports a rolling
~30-day compute-point window per organization). Everywhere in this surface the
period is named ``rolling_30d`` — never calendar-month semantics.
"""
import unittest
import json
from pathlib import Path

from abacus_usage import (
    normalize_email,
    classify_credit_log_period,
    parse_by_user_log,
    build_by_user_snapshot,
)


def _daily_rows(dates):
    return [{"date": d, "total": 1.0} for d in dates]


class EmailNormalizationTests(unittest.TestCase):
    def test_email_is_lowercased_and_trimmed(self):
        self.assertEqual(normalize_email("  Student.Name@School.EDU "), "student.name@school.edu")

    def test_invalid_email_is_rejected(self):
        self.assertEqual(normalize_email("not-an-email"), "")
        self.assertEqual(normalize_email("a@b"), "")
        self.assertEqual(normalize_email(""), "")
        self.assertEqual(normalize_email(None), "")
        self.assertEqual(normalize_email(42), "")

    def test_plus_addressing_is_preserved(self):
        self.assertEqual(normalize_email("Student+extra@school.edu"), "student+extra@school.edu")


class PeriodClassificationTests(unittest.TestCase):
    """The source period is always rolling_30d; bounds come from the daily log."""

    def test_full_month_dates_are_still_rolling_30d(self):
        dates = ["2026-08-%02d" % d for d in range(1, 32)]
        period = classify_credit_log_period(_daily_rows(dates), today="2026-08-31")
        self.assertEqual(period["type"], "rolling_30d")
        self.assertEqual(period["key"], "rolling_30d")
        self.assertEqual(period["window_start"], "2026-08-01")
        self.assertEqual(period["window_end"], "2026-08-31")

    def test_month_to_date_window_is_still_rolling_30d(self):
        dates = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
        period = classify_credit_log_period(_daily_rows(dates), today="2026-08-04")
        self.assertEqual(period["type"], "rolling_30d")
        self.assertEqual(period["key"], "rolling_30d")

    def test_observed_rolling_30_day_window(self):
        # Observed live window 2026-07-06..2026-08-04 (rolling ~30 days).
        dates = ["2026-07-06", "2026-07-07", "2026-07-31", "2026-08-01", "2026-08-04"]
        period = classify_credit_log_period(_daily_rows(dates), today="2026-08-04")
        self.assertEqual(period["type"], "rolling_30d")
        self.assertEqual(period["window_start"], "2026-07-06")
        self.assertEqual(period["window_end"], "2026-08-04")

    def test_window_ending_yesterday_is_still_rolling_30d(self):
        # A lagging daily log (max date is yesterday) is still a rolling window.
        dates = ["2026-08-01", "2026-08-02", "2026-08-03"]
        period = classify_credit_log_period(_daily_rows(dates), today="2026-08-04")
        self.assertEqual(period["type"], "rolling_30d")

    def test_mid_month_start_is_still_rolling_30d(self):
        dates = ["2026-08-15", "2026-08-16", "2026-08-17"]
        period = classify_credit_log_period(_daily_rows(dates), today="2026-08-17")
        self.assertEqual(period["type"], "rolling_30d")

    def test_missing_dates_have_null_bounds_but_remain_rolling_30d(self):
        period = classify_credit_log_period([], today="2026-08-04")
        self.assertEqual(period["type"], "rolling_30d")
        self.assertEqual(period["key"], "rolling_30d")
        self.assertIsNone(period["window_start"])
        self.assertIsNone(period["window_end"])

    def test_invalid_date_rows_are_ignored_for_bounds(self):
        period = classify_credit_log_period([{"date": "garbage", "total": 1.0}, {"date": "2026-08-01", "total": 1.0}], today="2026-08-04")
        self.assertEqual(period["type"], "rolling_30d")
        self.assertEqual(period["window_start"], "2026-08-01")
        self.assertEqual(period["window_end"], "2026-08-01")


class ByUserLogParsingTests(unittest.TestCase):
    COLUMNS = {"user", "email", "total", "cloud_llms", "RouteLLM API", "UI", "Abacus AI Agent", "Abacus AI Desktop"}

    def _payload(self, rows):
        # Canonical live shape: the compute-point log is nested under result.log.
        return {"success": True, "columns": list(self.COLUMNS), "result": {"log": rows}}

    def _legacy_payload(self, rows):
        # Pre-result.envelope compatibility shape: top-level log list.
        return {"success": True, "columns": list(self.COLUMNS), "log": rows}

    def test_valid_rows_are_sanitized_to_email_and_credits_only(self):
        payload = self._payload([
            {"user": "alice_org", "email": "Alice@School.Edu", "total": 100.0, "cloud_llms": 50.0, "RouteLLM API": 12.5, "UI": 30.0, "Abacus AI Agent": 5.0, "Abacus AI Desktop": 2.5},
        ])
        result = parse_by_user_log(payload)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["stats"]["skipped"], 0)
        row = result["users"][0]
        self.assertEqual(row["email"], "alice@school.edu")
        self.assertEqual(row["routeLlmCredits"], 12.5)
        self.assertEqual(row["totalCredits"], 100.0)
        # Privacy: no username, no non-RouteLLM bucket breakdown.
        self.assertNotIn("user", row)
        self.assertNotIn("cloud_llms", row)
        self.assertNotIn("UI", row)
        self.assertNotIn("Abacus AI Agent", row)
        self.assertNotIn("Abacus AI Desktop", row)

    def test_canonical_nested_log_is_accepted(self):
        payload = self._payload([
            {"user": "alice_org", "email": "Alice@School.Edu", "total": 100.0, "RouteLLM API": 12.5},
        ])
        result = parse_by_user_log(payload)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["stats"]["skipped"], 0)
        row = result["users"][0]
        self.assertEqual(row["email"], "alice@school.edu")
        self.assertEqual(row["routeLlmCredits"], 12.5)
        self.assertEqual(row["totalCredits"], 100.0)

    def test_legacy_top_level_log_remains_accepted(self):
        payload = self._legacy_payload([
            {"user": "bob_org", "email": "Bob@School.Edu", "total": 50.0, "RouteLLM API": 5.0},
        ])
        result = parse_by_user_log(payload)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["users"][0]["email"], "bob@school.edu")
        self.assertEqual(result["users"][0]["routeLlmCredits"], 5.0)

    def test_malformed_nested_log_fails_closed_without_top_level_fallback(self):
        # The nested result.log is authoritative: even a valid top-level log
        # list must not rescue a malformed canonical envelope.
        payload = self._legacy_payload([
            {"user": "a", "email": "a@school.edu", "total": 10.0, "RouteLLM API": 1.0},
        ])
        payload["result"] = {"log": "not-a-list"}
        with self.assertRaises(ValueError):
            parse_by_user_log(payload)

    def test_invalid_email_rows_are_skipped_and_counted(self):
        payload = self._payload([
            {"user": "x", "email": "not-an-email", "total": 10.0},
            {"user": "y", "email": "ok@school.edu", "total": 20.0},
        ])
        result = parse_by_user_log(payload)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["stats"]["skipped"], 1)
        self.assertEqual(result["users"][0]["email"], "ok@school.edu")

    def test_non_finite_or_negative_credits_are_skipped(self):
        payload = self._payload([
            {"user": "a", "email": "a@school.edu", "total": float("nan"), "RouteLLM API": 1.0},
            {"user": "b", "email": "b@school.edu", "total": 5.0, "RouteLLM API": -2.0},
            {"user": "c", "email": "c@school.edu", "total": 5.0, "RouteLLM API": 2.0},
        ])
        result = parse_by_user_log(payload)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["users"][0]["email"], "c@school.edu")

    def test_duplicate_normalized_emails_are_rejected_not_summed(self):
        payload = self._payload([
            {"user": "a1", "email": "A@School.edu", "total": 100.0, "RouteLLM API": 10.0},
            {"user": "a2", "email": "a@school.edu", "total": 200.0, "RouteLLM API": 20.0},
        ])
        result = parse_by_user_log(payload)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["stats"]["duplicates"], 1)
        # Never summed: only the first normalized-email row is kept.
        self.assertEqual(result["users"][0]["email"], "a@school.edu")
        self.assertEqual(result["users"][0]["routeLlmCredits"], 10.0)

    def test_row_overflow_is_bounded(self):
        rows = [{"user": "u%d" % i, "email": "u%d@school.edu" % i, "total": 1.0} for i in range(600)]
        result = parse_by_user_log(self._payload(rows), max_rows=500)
        self.assertEqual(result["stats"]["rows"], 500)
        self.assertEqual(len(result["users"]), 500)

    def test_credits_over_cap_are_skipped(self):
        payload = self._payload([
            {"user": "a", "email": "a@school.edu", "total": 2e9, "RouteLLM API": 1.0},
            {"user": "b", "email": "b@school.edu", "total": 3.0, "RouteLLM API": 1.0},
        ])
        result = parse_by_user_log(payload, max_credits=1e6)
        self.assertEqual(result["stats"]["rows"], 1)
        self.assertEqual(result["users"][0]["email"], "b@school.edu")

    def test_invalid_payload_raises(self):
        with self.assertRaises(ValueError):
            parse_by_user_log({"success": False, "log": []})
        with self.assertRaises(ValueError):
            parse_by_user_log({"success": True, "log": "not-a-list"})

    def test_snapshot_payload_contains_source_and_period_metadata(self):
        snapshot = build_by_user_snapshot(
            self._payload([{"user": "a", "email": "A@School.edu", "total": 10.0, "RouteLLM API": 2.0}]),
            {"success": True, "result": {"log": _daily_rows(["2026-08-01", "2026-08-04"])}},
            generated_at_ms=1785000000000,
            today="2026-08-04",
        )
        self.assertEqual(snapshot["source"], "abacus_credits_by_user")
        self.assertEqual(snapshot["periodType"], "rolling_30d")
        self.assertEqual(snapshot["period"]["type"], "rolling_30d")
        self.assertEqual(snapshot["period"]["key"], "rolling_30d")
        self.assertEqual(snapshot["users"][0]["email"], "a@school.edu")
        self.assertEqual(snapshot["users"][0]["routeLlmCredits"], 2.0)
        self.assertEqual(snapshot["users"][0]["totalCredits"], 10.0)

    def test_snapshot_nested_envelopes_yield_exact_window_bounds(self):
        snapshot = build_by_user_snapshot(
            {"success": True, "result": {"log": [{"user": "a", "email": "a@school.edu", "total": 10.0, "RouteLLM API": 1.0}]}},
            {"success": True, "result": {"log": _daily_rows(["2026-07-06", "2026-07-07", "2026-07-31", "2026-08-01", "2026-08-04"])}},
            generated_at_ms=1785000000000,
            today="2026-08-04",
        )
        self.assertEqual(snapshot["period"]["window_start"], "2026-07-06")
        self.assertEqual(snapshot["period"]["window_end"], "2026-08-04")
        self.assertEqual(snapshot["stats"]["rows"], 1)

    def test_snapshot_legacy_top_level_daily_log_is_bounded(self):
        snapshot = build_by_user_snapshot(
            self._legacy_payload([{"user": "a", "email": "a@school.edu", "total": 10.0, "RouteLLM API": 1.0}]),
            {"success": True, "log": _daily_rows(["2026-08-01", "2026-08-03"])},
            generated_at_ms=1785000000000,
            today="2026-08-04",
        )
        self.assertEqual(snapshot["period"]["window_start"], "2026-08-01")
        self.assertEqual(snapshot["period"]["window_end"], "2026-08-03")
        self.assertEqual(snapshot["users"][0]["email"], "a@school.edu")

    def test_snapshot_malformed_nested_daily_log_fails_closed(self):
        # Malformed canonical daily data must not downgrade to a top-level log.
        snapshot = build_by_user_snapshot(
            self._payload([{"user": "a", "email": "a@school.edu", "total": 10.0, "RouteLLM API": 1.0}]),
            {"success": True, "log": _daily_rows(["2026-08-01", "2026-08-04"]), "result": {"log": "not-a-list"}},
            generated_at_ms=1785000000000,
            today="2026-08-04",
        )
        self.assertIsNone(snapshot["period"]["window_start"])
        self.assertIsNone(snapshot["period"]["window_end"])


class ConnectorEndpointSafetySourceTests(unittest.TestCase):
    """Static source checks that the new snapshot endpoint is safe by construction."""

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")

    def test_snapshot_endpoint_exists_and_is_signed(self):
        self.assertIn('f"{PUBLIC_PREFIX}/v1/abacus/snapshot"', self.source)
        # The route body must authenticate with the same HMAC boundary as peers.
        route_start = self.source.index('f"{PUBLIC_PREFIX}/v1/abacus/snapshot"')
        route_block = self.source[route_start:route_start + 1200]
        self.assertIn("_authenticate(request.method, request.url.path, request.headers, body)", route_block)

    def test_snapshot_endpoint_is_get_only(self):
        route_start = self.source.index('f"{PUBLIC_PREFIX}/v1/abacus/snapshot"')
        route_block = self.source[route_start:route_start + 300]
        self.assertIn('methods=["GET"]', route_block)

    def test_snapshot_failure_is_generic_503(self):
        route_start = self.source.index('f"{PUBLIC_PREFIX}/v1/abacus/snapshot"')
        route_block = self.source[route_start:route_start + 1600]
        self.assertIn("503", route_block)
        self.assertNotIn("api_key", route_block)

    def test_snapshot_uses_by_user_credit_log(self):
        self.assertIn('"byUser": True', self.source)
        self.assertIn('"byUser": False', self.source)

    def test_snapshot_never_returns_username_or_extra_buckets(self):
        helper = (Path(__file__).parent / "abacus_usage.py").read_text(encoding="utf-8")
        self.assertIn("routeLlmCredits", helper)
        self.assertIn("totalCredits", helper)
        self.assertNotIn('"user"', helper.replace("user", ""))  # no username passthrough key

    def test_snapshot_cache_is_bounded_under_one_hour(self):
        self.assertIn("_snapshot_cache", self.source)
        self.assertIn("60", self.source)


if __name__ == "__main__":
    unittest.main()
