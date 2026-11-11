"""Focused offline tests. No test launches Codex or accesses real credentials."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.chatgpt_stats.collect import (
    AccountSettings,
    CollectorError,
    JsonlAppServer,
    atomic_update,
    child_environment,
    collect,
    main,
    sanitize_summary,
    settings_from_args,
    verify_identity,
)
from scripts.chatgpt_stats.render import SUMMARY_FIELDS


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
PERSONAL = AccountSettings("personal", "personal@example.test", "personal_profile")
WORK = AccountSettings("work", "work@example.test", "work.profile", "Example Workspace")
SUMMARY = {
    "lifetimeTokens": 12345678901,
    "peakDailyTokens": 680500001,
    "longestRunningTurnSec": 205201,
    "currentStreakDays": 48,
    "longestStreakDays": 49,
}


def source_data() -> dict:
    result = {"schemaVersion": 1, "accounts": {}}
    for name, handle in (("personal", "personal_profile"), ("work", "work.profile")):
        result["accounts"][name] = {
            "source": "screenshot",
            "fetchedAt": "2026-11-10T00:00:00Z",
            "profileHandle": handle,
            "summary": dict.fromkeys(SUMMARY_FIELDS),
            "snapshotDisplay": dict(
                zip(SUMMARY_FIELDS, ("9B", "680.5M", "2d 9h", "48 days", "48 days"))
            ),
        }
    return result


def account_response(settings: AccountSettings = PERSONAL) -> dict:
    return {
        "account": {
            "type": "chatgpt",
            "email": settings.expected_email,
            "planType": "pro" if settings.account == "personal" else "business",
            "workspaceName": settings.expected_workspace,
        },
        "requiresOpenaiAuth": True,
    }


class FakeTransport:
    def __init__(self, identity=None, usage=None, notification_after_identity=False):
        self.identity = identity if identity is not None else account_response()
        self.usage = usage if usage is not None else {"summary": deepcopy(SUMMARY)}
        self.notification_after_identity = notification_after_identity
        self.calls = []
        self.options = {}

    def factory(self, command, **options):
        self.command = command
        self.options = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "initialize":
            return {}
        if method == "account/read":
            return self.identity
        if method == "account/usage/read":
            if self.notification_after_identity:
                self.options["on_notification"]("account/updated", {"authMode": "chatgpt"})
            return self.usage
        raise AssertionError("Unexpected RPC")

    def notify(self, method, params=None):
        self.calls.append((method, params))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "usage.json"
        self.original = source_data()
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.before = self.path.read_bytes()

    def run_collector(self, fake, settings=PERSONAL, environment=None):
        return collect(
            settings,
            self.path,
            environment={} if environment is None else environment,
            now=NOW,
            transport_factory=fake.factory,
        )

    def test_personal_success_is_sanitized_preserves_work_and_uses_only_read_rpcs(self):
        usage = {
            "summary": {**SUMMARY, "privateEmail": "private@example.test"},
            "dailyUsageBuckets": [{"startDate": "2026-11-10", "tokens": 42}],
            "threadUsage": {"threadId": "private-thread"},
        }
        fake = FakeTransport(usage=usage)
        entry = self.run_collector(fake)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["accounts"]["work"], self.original["accounts"]["work"])
        self.assertEqual(saved["accounts"]["personal"], entry)
        self.assertEqual(entry["summary"], SUMMARY)
        self.assertEqual(entry["fetchedAt"], "2026-11-11T12:00:00Z")
        self.assertEqual(entry["source"], "account/usage/read")
        self.assertNotIn("snapshotDisplay", entry)
        for private in ("example.test", "private-thread", "dailyUsageBuckets", "workspaceName"):
            self.assertNotIn(private, self.path.read_text())
        self.assertEqual(
            [call[0] for call in fake.calls],
            ["initialize", "initialized", "account/read", "account/usage/read", "account/read"],
        )
        self.assertEqual(fake.calls[2][1], {"refreshToken": False})
        self.assertEqual(fake.calls[4][1], {"refreshToken": False})

    def test_work_token_is_child_only_and_not_in_argv_or_public_output(self):
        fake = FakeTransport(identity=account_response(WORK))
        environment = {
            "CHATGPT_WORK_ACCESS_TOKEN": "synthetic-work-secret",
            "OPENAI_API_KEY": "synthetic-api-secret",
            "PATH": "/bin",
        }
        self.run_collector(fake, WORK, environment)
        self.assertEqual(fake.options["environment"]["CODEX_ACCESS_TOKEN"], "synthetic-work-secret")
        self.assertNotIn("CHATGPT_WORK_ACCESS_TOKEN", fake.options["environment"])
        self.assertNotIn("OPENAI_API_KEY", fake.options["environment"])
        self.assertIn('cli_auth_credentials_store="ephemeral"', fake.command)
        self.assertNotIn("synthetic-work-secret", " ".join(fake.command))
        self.assertNotIn("synthetic-work-secret", self.path.read_text())
        self.assertNotIn("CODEX_ACCESS_TOKEN", environment)
        self.assertEqual(json.loads(self.path.read_text())["accounts"]["personal"], self.original["accounts"]["personal"])

    def test_failed_identity_never_reads_usage_or_updates_data(self):
        variants = []
        for field, value in (("email", "another@example.test"), ("email", None), ("planType", "business"), ("planType", "unknown"), ("type", "apiKey")):
            identity = account_response()
            identity["account"][field] = value
            variants.append(identity)
        variants.append({"account": None})
        for identity in variants:
            with self.subTest(identity=identity):
                fake = FakeTransport(identity=identity)
                with self.assertRaises(CollectorError):
                    self.run_collector(fake)
                self.assertNotIn("account/usage/read", [call[0] for call in fake.calls])
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_work_workspace_and_plan_are_required_to_match(self):
        for field, value in (("workspaceName", None), ("workspaceName", "Other Workspace"), ("planType", "pro")):
            with self.subTest(field=field, value=value):
                identity = account_response(WORK)
                identity["account"][field] = value
                with self.assertRaises(CollectorError):
                    verify_identity(identity, WORK)

    def test_mapping_changes_and_duplicate_handles_fail_before_startup(self):
        for handles in (("wrong_profile", "work.profile"), ("personal_profile", "@PERSONAL_PROFILE")):
            with self.subTest(handles=handles):
                data = source_data()
                data["accounts"]["personal"]["profileHandle"] = handles[0]
                data["accounts"]["work"]["profileHandle"] = handles[1]
                self.path.write_text(json.dumps(data))
                fake = FakeTransport()
                with self.assertRaises(CollectorError):
                    self.run_collector(fake)
                self.assertFalse(fake.calls)

    def test_invalid_metrics_leave_previous_snapshot_byte_for_byte(self):
        for value in (True, -1, 1.5, "12", float("nan"), 2**63):
            with self.subTest(value=value):
                fake = FakeTransport(usage={"summary": {**SUMMARY, "lifetimeTokens": value}})
                with self.assertRaises(CollectorError):
                    self.run_collector(fake)
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_null_or_absent_metric_stays_unavailable_and_zero_remains_zero(self):
        self.assertEqual(
            sanitize_summary({"summary": {"lifetimeTokens": 0}}),
            {field: 0 if field == "lifetimeTokens" else None for field in SUMMARY_FIELDS},
        )
        with self.assertRaises(CollectorError):
            sanitize_summary({"summary": {}})

    def test_auth_change_during_collection_fails_closed(self):
        with self.assertRaises(CollectorError):
            self.run_collector(FakeTransport(notification_after_identity=True))
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_auth_change_after_usage_is_detected_before_save(self):
        class PostUsageIdentityChange(FakeTransport):
            def request(self, method, params=None):
                if method == "account/read" and any(
                    call[0] == "account/usage/read" for call in self.calls
                ):
                    self.options["on_notification"](
                        "account/updated", {"authMode": "chatgpt"}
                    )
                return super().request(method, params)

        fake = PostUsageIdentityChange()
        with self.assertRaisesRegex(CollectorError, "Authentication changed"):
            self.run_collector(fake)
        self.assertIn("account/usage/read", [call[0] for call in fake.calls])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_private_keys_in_existing_public_data_are_rejected(self):
        self.original["accounts"]["personal"]["email"] = "private@example.test"
        self.path.write_text(json.dumps(self.original))
        fake = FakeTransport()
        with self.assertRaises(CollectorError):
            self.run_collector(fake)
        self.assertFalse(fake.calls)

    def test_atomic_failure_keeps_existing_file_and_cleans_temporary_files(self):
        entry = {
            "source": "account/usage/read",
            "fetchedAt": "2026-11-11T12:00:00Z",
            "profileHandle": "personal_profile",
            "summary": SUMMARY,
        }
        with patch("scripts.chatgpt_stats.collect.os.replace", side_effect=OSError("private detail")):
            with self.assertRaises(CollectorError) as raised:
                atomic_update(self.path, PERSONAL, entry, now=NOW)
        self.assertNotIn("private detail", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class IdentityConfigTests(unittest.TestCase):
    def test_environment_isolation_and_required_credentials(self):
        for token in ("secret", ""):
            with self.subTest(token=token), self.assertRaises(CollectorError):
                child_environment(PERSONAL, {"CODEX_ACCESS_TOKEN": token})
        with self.assertRaises(CollectorError):
            child_environment(WORK, {})
        result = child_environment(PERSONAL, {"CHATGPT_WORK_ACCESS_TOKEN": "secret", "HOME": "/unchanged", "CODEX_HOME": "/also-unchanged"})
        self.assertNotIn("CHATGPT_WORK_ACCESS_TOKEN", result)
        self.assertEqual(result["HOME"], "/unchanged")
        self.assertEqual(result["CODEX_HOME"], "/also-unchanged")

    def test_private_config_root_shape_and_no_secret_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "accounts.json"
            config = {"personal": {"expectedEmail": PERSONAL.expected_email, "profileHandle": PERSONAL.profile_handle}}
            path.write_text(json.dumps(config))
            args = argparse.Namespace(account="personal", config=path, expected_email=None, profile_handle=None, expected_workspace=None)
            self.assertEqual(settings_from_args(args), PERSONAL)
            config["work"] = {"token": "synthetic-secret"}
            path.write_text(json.dumps(config))
            with self.assertRaises(CollectorError):
                settings_from_args(args)

    def test_missing_identity_is_a_safe_cli_failure(self):
        with patch("sys.stderr", new_callable=io.StringIO) as error:
            result = main(["--account", "personal", "--data", "/unused/path.json"])
        self.assertEqual(result, 1)
        self.assertIn("expected account email", error.getvalue())


class JsonlTransportTests(unittest.TestCase):
    def transport(self, code, timeout=1):
        # The fake protocol server is a fresh Python subprocess, never Codex.
        return JsonlAppServer([sys.executable, "-u", "-c", code], environment={}, timeout=timeout)

    def test_real_pipes_handle_notifications_and_correlated_responses(self):
        code = """import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if 'id' in request:
        print(json.dumps({'method':'notice','params':{'private':'not logged'}}),flush=True)
        print(json.dumps({'id':request['id'],'result':{'ok':True}}),flush=True)
"""
        notices = []
        with self.transport(code) as transport:
            transport.on_notification = lambda method, params: notices.append(method)
            self.assertEqual(transport.request("initialize", {}), {"ok": True})
            transport.notify("initialized", {})
            self.assertEqual(transport.request("account/read", {"refreshToken": False}), {"ok": True})
        self.assertEqual(notices, ["notice", "notice"])

    def test_upstream_error_and_stderr_do_not_leak(self):
        code = """import json,sys
request=json.loads(sys.stdin.readline())
print('synthetic-sensitive-stderr',file=sys.stderr,flush=True)
print(json.dumps({'id':request['id'],'error':{'message':'synthetic-sensitive-rpc','code':-1}}),flush=True)
"""
        with self.transport(code) as transport:
            with self.assertRaises(CollectorError) as raised:
                transport.request("initialize")
        self.assertNotIn("synthetic-sensitive", str(raised.exception))

    def test_timeout_is_bounded_and_child_is_stopped(self):
        with self.transport("import time; time.sleep(10)", timeout=0.05) as transport:
            process = transport._process
            with self.assertRaisesRegex(CollectorError, "timed out"):
                transport.request("initialize")
        self.assertIsNotNone(process.poll())

    def test_malformed_and_unsolicited_rpc_responses_are_rejected(self):
        payloads = (
            "not-json-with-sensitive-content",
            '{"id":1,"id":2,"result":{}}',
            '{"id":true,"result":{}}',
            '{"id":1,"result":{},"error":{}}',
            '{"id":2,"result":{}}',
            '{"id":99,"method":"account/chatgptAuthTokens/refresh","params":{}}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                code = f"import sys; sys.stdin.readline(); print({payload!r},flush=True)"
                with self.transport(code) as transport:
                    with self.assertRaises(CollectorError) as raised:
                        transport.request("initialize")
                self.assertNotIn(payload, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
