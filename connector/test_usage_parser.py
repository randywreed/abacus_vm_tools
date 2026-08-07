"""Focused safety tests for Abacus credit discovery and normalization."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from abacus_usage import abacus_api_key_from_config, balance_credit_report, daily_credit_report
from telemetry import normalize_usage


class UsageParserTests(unittest.TestCase):
    def test_current_top_level_model_shape(self):
        key = abacus_api_key_from_config("model:\n  base_url: https://routellm.abacus.ai/v1\n  api_key: s2_top_secret\ncustom_providers:\n  - name: custom\n")
        self.assertEqual(key, "s2_top_secret")

    def test_custom_provider_uses_base_url_not_name(self):
        key = abacus_api_key_from_config("custom_providers:\n  - name: arbitrary-label\n    base_url: https://routellm.abacus.ai/v1\n    api_key: s2_custom_secret\n")
        self.assertEqual(key, "s2_custom_secret")

    def test_similar_hostname_is_rejected(self):
        key = abacus_api_key_from_config("model:\n  base_url: https://routellm.abacus.ai.evil.example/v1\n  api_key: s2_do_not_select\n")
        self.assertEqual(key, "")

    def test_non_https_routellm_url_is_rejected(self):
        self.assertEqual(abacus_api_key_from_config("model:\n  base_url: http://routellm.abacus.ai/v1\n  api_key: s2_do_not_select\n"), "")

    def test_missing_key_is_empty_and_errors_do_not_include_secrets(self):
        self.assertEqual(abacus_api_key_from_config("model:\n  base_url: https://routellm.abacus.ai/v1\n"), "")
        with self.assertRaisesRegex(ValueError, "invalid") as raised:
            balance_credit_report({"success": False, "error": "s2_do_not_expose"})
        self.assertNotIn("s2_do_not_expose", str(raised.exception))

    def test_balance_hundredths_and_daily_display_units(self):
        report = balance_credit_report({"success": True, "result": {"organization": {"computePointInfo": {"currMonthAvailPoints": 3000, "currMonthUsage": 125, "last24HoursUsage": 50, "last7DaysUsage": 250, "updatedAt": "2026-07-25T00:00:00Z"}}}})
        self.assertEqual(report["cycleTotalCredits"], 30.0)
        self.assertEqual(report["cycleUsedCredits"], 1.25)
        self.assertEqual(report["cycleRemainingCredits"], 28.75)
        daily = daily_credit_report({"success": True, "result": {"log": [{"date": "2026-07-25", "total": 8.39, "cloud_llms": 8.39}]}}, "2026-07-25")
        self.assertEqual(daily["totalCredits"], 8.39)
        self.assertEqual(daily["sourceBuckets"], {"cloud_llms": 8.39})

    def test_incomplete_balance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            balance_credit_report({"success": True, "result": {}})

    def test_missing_or_nonfinite_required_points_are_rejected(self):
        base = {"success": True, "result": {"organization": {"computePointInfo": {"currMonthAvailPoints": 1}}}}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            balance_credit_report(base)
        base["result"]["organization"]["computePointInfo"]["currMonthUsage"] = float("nan")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            balance_credit_report(base)

    def test_installer_explicitly_restarts_updated_connector(self):
        installer = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        self.assertIn("hermes_classroom_connector.py", installer)
        self.assertIn("idempotency.py", installer)
        self.assertIn("session_payloads.py", installer)
        self.assertIn("streaming_sse.py", installer)
        self.assertIn("hermes-classroom-connector.service", installer)
        enable_line = next(
            line for line in installer.splitlines() if line.strip().startswith("systemctl enable")
        )
        self.assertIn("hermes-classroom-connector.service", enable_line)
        self.assertIn("systemctl restart hermes-classroom-connector.service", installer)
        self.assertNotIn("systemctl enable --now hermes-classroom-connector.service", installer)

    def test_hermes_usage_aliases_preserve_absent_vs_zero_and_reported_values(self):
        reported = normalize_usage({"input_tokens": 12, "output_tokens": 3, "model_calls": 2, "credits": 0.4, "cost": 0})
        self.assertEqual(reported["prompt_tokens"], 12)
        self.assertEqual(reported["completion_tokens"], 3)
        self.assertEqual(reported["total_tokens"], 15)
        self.assertEqual(reported["telemetry"], {"tokens_reported": True, "input_output_reported": True, "model_calls": 2, "credits": 0.4, "cost": 0.0})
        absent = normalize_usage({})
        self.assertIsNone(absent["prompt_tokens"])
        self.assertFalse(absent["telemetry"]["tokens_reported"])

    def test_current_gateway_usage_aliases_are_normalized(self):
        reported = normalize_usage({
            "input": 12,
            "output": 3,
            "prompt": 12,
            "completion": 3,
            "total": 15,
            "calls": 2,
        })
        self.assertEqual(reported["prompt_tokens"], 12)
        self.assertEqual(reported["completion_tokens"], 3)
        self.assertEqual(reported["total_tokens"], 15)
        self.assertEqual(reported["telemetry"]["model_calls"], 2)
        self.assertTrue(reported["telemetry"]["tokens_reported"])
        self.assertTrue(reported["telemetry"]["input_output_reported"])

    def test_total_only_usage_does_not_claim_input_output_breakdown(self):
        total_only = normalize_usage({"total_tokens": 42})
        self.assertTrue(total_only["telemetry"]["tokens_reported"])
        self.assertFalse(total_only["telemetry"]["input_output_reported"])
        self.assertIsNone(total_only["prompt_tokens"])
        self.assertIsNone(total_only["completion_tokens"])


if __name__ == "__main__":
    unittest.main()
