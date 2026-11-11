"""Offline correctness checks for publishing public aggregate stats."""

import base64
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts" / "chatgpt_stats"
sys.path.insert(0, str(SCRIPT_DIR))
import publish
import render


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
OLD_SHA = "a" * 40
BASE_TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_SHA = "d" * 40
METRICS = {"lifetimeTokens": 1234567890, "peakDailyTokens": 23456,
           "longestRunningTurnSec": 90000, "currentStreakDays": 5,
           "longestStreakDays": 8}


def fixture():
    return {"schemaVersion": 1, "accounts": {
        "personal": {"source": "account/usage/read", "fetchedAt": "2026-11-11T12:00:00Z",
                     "profileHandle": "jmhenn_", "summary": copy.deepcopy(METRICS)},
        "work": {"source": "account/usage/read", "fetchedAt": "2026-11-11T12:00:00Z",
                 "profileHandle": "james.hennahane", "summary": copy.deepcopy(METRICS)},
    }}


def encoded_file(text):
    return {"type": "file", "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode()}


class FakeGitHub:
    def __init__(self, data=None, readme=None, fail_update=False):
        self.calls = []
        self.data = data if data is not None else fixture()
        self.readme = readme or ("# Keep this\n\n<!-- CHATGPT_ACTIVITY_START -->\nold stats\n"
                                 "<!-- CHATGPT_ACTIVITY_END -->\n\n![Existing badge](https://example.test/badge.svg)\n")
        self.fail_update = fail_update

    def request(self, method, endpoint, payload=None):
        self.calls.append((method, endpoint, payload))
        if method == "GET" and "/git/ref/heads/" in endpoint:
            return {"object": {"sha": OLD_SHA}}
        if method == "GET" and endpoint.endswith(f"/git/commits/{OLD_SHA}"):
            return {"tree": {"sha": BASE_TREE}}
        if method == "GET" and "/contents/README.md?" in endpoint:
            return encoded_file(self.readme)
        if method == "GET" and "/contents/data/chatgpt-usage.json?" in endpoint:
            return encoded_file(json.dumps(self.data, indent=2) + "\n")
        if method == "POST" and endpoint.endswith("/git/trees"):
            return {"sha": NEW_TREE}
        if method == "POST" and endpoint.endswith("/git/commits"):
            return {"sha": NEW_SHA}
        if method == "PATCH":
            if self.fail_update:
                raise publish.PublishError("Concurrent update rejected.")
            return {"object": {"sha": NEW_SHA}}
        raise AssertionError((method, endpoint))


class MergeTests(unittest.TestCase):
    def test_newer_account_merges_without_rolling_back_the_other(self):
        remote = fixture()
        remote["accounts"]["work"]["fetchedAt"] = "2026-11-12T08:00:00Z"
        incoming = fixture()
        incoming["accounts"]["personal"]["fetchedAt"] = "2026-11-12T10:00:00Z"
        incoming["accounts"]["personal"]["summary"]["lifetimeTokens"] += 10
        merged = publish.merge_data(remote, incoming, now=NOW)
        self.assertEqual(merged["accounts"]["personal"], incoming["accounts"]["personal"])
        self.assertEqual(merged["accounts"]["work"], remote["accounts"]["work"])
        self.assertNotEqual(remote["accounts"]["personal"], merged["accounts"]["personal"])

    def test_same_timestamp_conflict_fails_closed(self):
        incoming = fixture()
        incoming["accounts"]["personal"]["summary"]["lifetimeTokens"] += 1
        with self.assertRaises(publish.PublishError):
            publish.merge_data(fixture(), incoming, now=NOW)

    def test_profile_cannot_be_silently_remapped(self):
        incoming = fixture()
        incoming["accounts"]["personal"]["profileHandle"] = "another_person"
        with self.assertRaises((publish.PublishError, ValueError)):
            publish.merge_data(fixture(), incoming, now=NOW)

    def test_manual_snapshot_cannot_replace_api_data(self):
        incoming = fixture()
        personal = incoming["accounts"]["personal"]
        personal["source"] = "screenshot"
        personal["fetchedAt"] = "2026-11-12T10:00:00Z"
        personal["summary"] = dict.fromkeys(METRICS)
        personal["snapshotDisplay"] = {"lifetimeTokens": "9B", "peakDailyTokens": "680.5M",
                                       "longestRunningTurnSec": "2d 9h", "currentStreakDays": "48 days",
                                       "longestStreakDays": "48 days"}
        with self.assertRaises(publish.PublishError):
            publish.merge_data(fixture(), incoming, now=NOW)

    def test_unexpected_public_fields_are_rejected(self):
        incoming = fixture()
        incoming["accounts"]["personal"]["email"] = "private@example.test"
        with self.assertRaises(ValueError):
            publish.merge_data(fixture(), incoming, now=NOW)


class PublishingTests(unittest.TestCase):
    def test_prepare_is_read_only_and_pins_both_files_to_head(self):
        client = FakeGitHub()
        plan = publish.prepare_plan(client, incoming=fixture(), now=NOW)
        self.assertTrue(plan.changed_files)
        self.assertTrue(all(method == "GET" for method, _, _ in client.calls))
        contents_calls = [path for _, path, _ in client.calls if "/contents/" in path]
        self.assertEqual(len(contents_calls), 2)
        self.assertTrue(all(path.endswith(f"?ref={OLD_SHA}") for path in contents_calls))
        self.assertTrue(plan.new_readme.startswith("# Keep this\n\n<!-- CHATGPT_ACTIVITY_START -->"))
        self.assertTrue(plan.new_readme.endswith("![Existing badge](https://example.test/badge.svg)\n"))

    def test_publish_changes_only_allowed_files_and_never_forces(self):
        client = FakeGitHub()
        incoming = fixture()
        incoming["accounts"]["personal"]["fetchedAt"] = "2026-11-12T10:00:00Z"
        plan = publish.prepare_plan(client, incoming=incoming, now=NOW)
        sha = publish.publish_plan(client, plan)
        self.assertEqual(sha, NEW_SHA)
        writes = [(method, path, payload) for method, path, payload in client.calls if method != "GET"]
        self.assertEqual([method for method, _, _ in writes], ["POST", "POST", "PATCH"])
        tree = writes[0][2]
        self.assertEqual(tree["base_tree"], BASE_TREE)
        self.assertEqual({item["path"] for item in tree["tree"]}, {"README.md", "data/chatgpt-usage.json"})
        self.assertEqual(writes[1][2]["parents"], [OLD_SHA])
        self.assertEqual(writes[2][2], {"sha": NEW_SHA, "force": False})

    def test_concurrent_update_does_not_retry_or_force(self):
        client = FakeGitHub(fail_update=True)
        plan = publish.prepare_plan(client, incoming=fixture(), now=NOW)
        with self.assertRaises(publish.PublishError):
            publish.publish_plan(client, plan)
        updates = [call for call in client.calls if call[0] == "PATCH"]
        self.assertEqual(len(updates), 1)
        self.assertIs(updates[0][2]["force"], False)

    def test_no_changes_perform_no_writes(self):
        data = fixture()
        base = FakeGitHub().readme
        current = render.update_readme(base, data, now=NOW)
        client = FakeGitHub(data=data, readme=current)
        plan = publish.prepare_plan(client, now=NOW)
        self.assertEqual(plan.changed_files, {})
        self.assertIsNone(publish.publish_plan(client, plan))
        self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_missing_markers_abort_before_writes(self):
        client = FakeGitHub(readme="# Unmarked README\n")
        with self.assertRaises(ValueError):
            publish.prepare_plan(client, incoming=fixture(), now=NOW)
        self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_refresh_only_retains_data(self):
        client = FakeGitHub()
        plan = publish.prepare_plan(client, incoming=None, now=NOW)
        self.assertEqual(plan.new_data, plan.old_data)
        self.assertEqual(set(plan.changed_files), {"README.md"})


class TransportTests(unittest.TestCase):
    @patch("publish.subprocess.run")
    def test_json_uses_stdin_without_shell_and_has_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"sha":"ok"}', stderr="")
        payload = {"content": "literal $(do-not-execute) `also-literal`\nnext line"}
        publish.GitHubClient().request("POST", "repos/hendoctor/hendoctor/git/trees", payload)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(json.loads(kwargs["input"]), payload)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 30)

    @patch("publish.subprocess.run")
    def test_auth_diagnostics_are_not_exposed(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="SECRET_TOKEN_VALUE")
        with self.assertRaises(publish.PublishError) as caught:
            publish.GitHubClient().request("GET", "repos/hendoctor/hendoctor/git/ref/heads/main")
        self.assertNotIn("SECRET_TOKEN_VALUE", str(caught.exception))

    @patch("publish.subprocess.run")
    def test_timeout_is_sanitized(self, run):
        run.side_effect = subprocess.TimeoutExpired("gh secret", 30, stderr="SECRET_TOKEN_VALUE")
        with self.assertRaises(publish.PublishError) as caught:
            publish.GitHubClient().request("GET", "repos/hendoctor/hendoctor/git/ref/heads/main")
        self.assertNotIn("SECRET_TOKEN_VALUE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
