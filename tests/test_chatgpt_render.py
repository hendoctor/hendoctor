"""Focused schema, disclosure, and surgical README-update tests."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "chatgpt_stats" / "render.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_stats_render", SCRIPT)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def snapshot_data():
    accounts = {}
    for name, handle, values in (
        ("personal", "@jmhenn_", ("9B", "680.5M", "2d 9h", "48 days", "48 days")),
        ("work", "@james.hennahane", ("3.8B", "160.6M", "6d 13h", "58 days", "58 days")),
    ):
        accounts[name] = {
            "source": "screenshot",
            "fetchedAt": "2026-11-11T00:00:00Z",
            "profileHandle": handle,
            "summary": dict.fromkeys(renderer.SUMMARY_FIELDS),
            "snapshotDisplay": dict(zip(renderer.SUMMARY_FIELDS, values)),
        }
    return {"schemaVersion": 1, "accounts": accounts}


def with_personal_api():
    data = snapshot_data()
    account = data["accounts"]["personal"]
    account["source"] = "account/usage/read"
    account.pop("snapshotDisplay")
    account["summary"] = dict(zip(renderer.SUMMARY_FIELDS, (9_123_456_789, 680_500_001, 205_201, 1, 48)))
    return data


def readme(newline="\n"):
    return newline.join((
        "# Existing profile", "![Keep](https://example.invalid/image.svg)",
        renderer.START_MARKER, "Old stats", renderer.END_MARKER,
        "", "## Original beliefs", "Keep everything here.", "",
    ))


class ValidationTests(unittest.TestCase):
    def test_snapshot_is_valid_without_becoming_exact_or_mutating(self):
        data = snapshot_data()
        before = deepcopy(data)
        self.assertIs(renderer.validate_data(data, now=NOW), data)
        self.assertEqual(data, before)
        for account in data["accounts"].values():
            self.assertTrue(all(value is None for value in account["summary"].values()))

    def test_rejects_unknown_fields_at_every_public_object_level(self):
        for path in ((), ("accounts",), ("accounts", "personal"),
                     ("accounts", "personal", "summary"),
                     ("accounts", "personal", "snapshotDisplay")):
            with self.subTest(path=path):
                data = snapshot_data()
                target = data
                for key in path:
                    target = target[key]
                target["private_credential"] = "DO_NOT_ECHO_THIS"
                with self.assertRaises(renderer.ValidationError) as caught:
                    renderer.validate_data(data, now=NOW)
                self.assertNotIn("private_credential", str(caught.exception))
                self.assertNotIn("DO_NOT_ECHO_THIS", str(caught.exception))

    def test_metrics_reject_bools_floats_negatives_and_unsafe_integers(self):
        for key in renderer.SUMMARY_FIELDS:
            for value in (True, False, -1, 1.0, "1", 2**53, float("nan"), {}):
                with self.subTest(key=key, value=value):
                    data = with_personal_api()
                    data["accounts"]["personal"]["summary"][key] = value
                    with self.assertRaises(renderer.ValidationError):
                        renderer.validate_data(data, now=NOW)

    def test_nullable_zero_and_maximum_safe_metrics_are_valid(self):
        for value in (None, 0, 2**53 - 1):
            data = with_personal_api()
            data["accounts"]["personal"]["summary"] = dict.fromkeys(renderer.SUMMARY_FIELDS, value)
            renderer.validate_data(data, now=NOW)

    def test_missing_schema_account_and_metric_fields_fail(self):
        mutations = (
            lambda data: data.pop("schemaVersion"),
            lambda data: data["accounts"].pop("work"),
            lambda data: data["accounts"]["personal"]["summary"].pop("lifetimeTokens"),
            lambda data: data.update(schemaVersion=True),
            lambda data: data.update(schemaVersion=2),
        )
        for mutate in mutations:
            data = snapshot_data()
            mutate(data)
            with self.assertRaises(renderer.ValidationError):
                renderer.validate_data(data, now=NOW)

    def test_screenshot_cannot_claim_exact_metrics_or_inject_markdown(self):
        data = snapshot_data()
        data["accounts"]["personal"]["summary"]["lifetimeTokens"] = 9_000_000_000
        with self.assertRaises(renderer.ValidationError):
            renderer.validate_data(data, now=NOW)
        for field in renderer.SUMMARY_FIELDS:
            data = snapshot_data()
            data["accounts"]["personal"]["snapshotDisplay"][field] = "1 | [secret](https://example.invalid)"
            with self.assertRaises(renderer.ValidationError):
                renderer.validate_data(data, now=NOW)

    def test_api_forbids_snapshot_display_and_handles_cannot_inject_links(self):
        data = snapshot_data()
        data["accounts"]["personal"]["source"] = "account/usage/read"
        with self.assertRaises(renderer.ValidationError):
            renderer.validate_data(data, now=NOW)
        data = snapshot_data()
        data["accounts"]["personal"]["profileHandle"] = "x](https://example.invalid)"
        with self.assertRaises(renderer.ValidationError):
            renderer.validate_data(data, now=NOW)

    def test_utc_timestamps_and_future_tolerance(self):
        for value in ("2026-11-11", "2026-11-11T12:00:00", "2026-11-11T12:00:00+01:00",
                      "2026-02-30T00:00:00Z", "2026-11-11T12:05:01Z"):
            data = snapshot_data()
            data["accounts"]["personal"]["fetchedAt"] = value
            with self.subTest(value=value), self.assertRaises(renderer.ValidationError):
                renderer.validate_data(data, now=NOW)
        for value in ("2026-11-11T12:05:00Z", "2026-11-11T12:00:00.123456+00:00"):
            data = snapshot_data()
            data["accounts"]["personal"]["fetchedAt"] = value
            renderer.validate_data(data, now=NOW)


class RenderingTests(unittest.TestCase):
    def test_snapshot_keeps_original_displays_badges_and_explicit_sources(self):
        section = renderer.render_section(snapshot_data(), now=NOW)
        self.assertIn("| Lifetime tokens | 9B | 3.8B |", section)
        self.assertIn("| Peak tokens | 680.5M | 160.6M |", section)
        self.assertIn("| Longest task | 2d 9h | 6d 13h |", section)
        self.assertIn("| Streak at last sync | 48 days | 58 days |", section)
        self.assertEqual(section.count("Manual screenshot snapshot"), 2)
        self.assertIn("| Last recorded | 2026-11-11 00:00 UTC | 2026-11-11 00:00 UTC |", section)
        self.assertIn("Screenshot values are manually transcribed, rounded display values.", section)
        self.assertIn("Personal_ChatGPT-9B_lifetime_tokens-166534", section)
        self.assertIn("Work_ChatGPT-3.8B_lifetime_tokens-0078D4", section)
        self.assertNotIn("Combined", section)
        self.assertNotIn("12.8B", section)
        self.assertIn("[@jmhenn_](https://chatgpt.com/u/jmhenn_)", section)
        self.assertNotIn("https://chatgpt.com/u/james.hennahane", section)

    def test_one_api_account_does_not_upgrade_the_other_snapshot(self):
        section = renderer.render_section(with_personal_api(), now=NOW)
        self.assertIn("| Lifetime tokens | 9,123,456,789 | 3.8B |", section)
        self.assertIn("| Longest task | 2d 9h 1s | 6d 13h |", section)
        self.assertIn("| Streak at last sync | 1 day | 58 days |", section)
        self.assertIn("| Source | ChatGPT account usage | Manual screenshot snapshot |", section)
        self.assertNotIn("account/usage/read", section)
        self.assertIn("Screenshot values are manually transcribed, rounded display values.", section)
        self.assertIn("9%2C123%2C456%2C789_lifetime_tokens", section)

    def test_all_api_footer_omits_screenshot_language(self):
        data = with_personal_api()
        work = data["accounts"]["work"]
        work["source"] = "account/usage/read"
        work.pop("snapshotDisplay")
        work["summary"] = dict.fromkeys(renderer.SUMMARY_FIELDS)
        section = renderer.render_section(data, now=NOW)
        self.assertNotIn("screenshot", section.lower())
        self.assertNotIn("account/usage/read", section)
        self.assertIn("Values reflect the latest successful account refresh; missing metrics are unavailable.", section)
        self.assertIn("Streaks reflect the last recorded sync.", section)

    def test_null_values_and_zero_durations_render_without_invention(self):
        data = with_personal_api()
        data["accounts"]["personal"]["summary"] = dict.fromkeys(renderer.SUMMARY_FIELDS)
        data["accounts"]["personal"]["summary"]["longestRunningTurnSec"] = 0
        section = renderer.render_section(data, now=NOW)
        self.assertIn("| Lifetime tokens | Unavailable | 3.8B |", section)
        self.assertIn("| Longest task | 0s | 6d 13h |", section)

    def test_staleness_is_per_account_and_strictly_over_48_hours(self):
        data = snapshot_data()
        data["accounts"]["personal"]["fetchedAt"] = "2026-11-09T11:59:59Z"
        data["accounts"]["work"]["fetchedAt"] = "2026-11-09T12:00:00Z"
        section = renderer.render_section(data, now=NOW)
        self.assertIn("| Freshness | **Stale (>48h)** | Recorded within 48h |", section)

    def test_update_preserves_everything_outside_markers_and_is_idempotent(self):
        for newline in ("\n", "\r\n"):
            before = readme(newline)
            after = renderer.update_readme(before, snapshot_data(), now=NOW)
            self.assertEqual(before.split(renderer.START_MARKER)[0], after.split(renderer.START_MARKER)[0])
            self.assertEqual(before.split(renderer.END_MARKER)[1], after.split(renderer.END_MARKER)[1])
            self.assertEqual(after, renderer.update_readme(after, snapshot_data(), now=NOW))
            if newline == "\r\n":
                self.assertNotIn("\n", after.replace("\r\n", ""))

    def test_missing_duplicate_reversed_and_inline_markers_are_rejected(self):
        cases = (
            "No markers", readme().replace(renderer.END_MARKER, ""),
            readme() + renderer.START_MARKER,
            renderer.END_MARKER + "\n" + renderer.START_MARKER,
            "prefix " + renderer.START_MARKER + "\n" + renderer.END_MARKER,
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(renderer.ValidationError):
                renderer.update_readme(text, snapshot_data(), now=NOW)


class CliTests(unittest.TestCase):
    def test_check_never_writes_and_update_then_check_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme_path, data_path = root / "README.md", root / "stats.json"
            original = readme("\r\n").encode("utf-8")
            readme_path.write_bytes(original)
            data_path.write_text(json.dumps(snapshot_data()), encoding="utf-8")
            command = [sys.executable, str(SCRIPT), "--readme", str(readme_path),
                       "--data", str(data_path), "--now", "2026-11-11T12:00:00Z"]
            checked = subprocess.run(command + ["--check"], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 1, checked.stderr)
            self.assertEqual(readme_path.read_bytes(), original)
            updated = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(updated.returncode, 0, updated.stderr)
            checked = subprocess.run(command + ["--check"], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            saved = readme_path.read_bytes()
            data_path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding="utf-8")
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(readme_path.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
