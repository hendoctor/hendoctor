#!/usr/bin/env python3
"""Read verified ChatGPT account statistics through the local Codex app-server.

Run on trusted private infrastructure. This client never opens authentication
files, performs a login, starts a thread, or asks a model to do work. Codex owns
the managed personal session and any normal credential refresh. Only the five
allowlisted summary metrics and public profile metadata reach the output file.

Work requires a member's Codex access token in CHATGPT_WORK_ACCESS_TOKEN. Its
creator and workspace must be verified at setup; account/read currently exposes
email and a non-unique workspace display name, not immutable identity IDs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

try:
    from .render import SUMMARY_FIELDS, validate_data
except ImportError:  # Support direct execution: python scripts/chatgpt_stats/collect.py
    from render import SUMMARY_FIELDS, validate_data


CONSUMER_PLANS = frozenset({"free", "go", "plus", "pro", "prolite", "promax"})
WORK_PLANS = frozenset(
    {
        "team",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "business",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "enterprise",
    }
)
MAX_INT64 = (1 << 63) - 1
MAX_JSON_BYTES = 1024 * 1024
_EOF = object()
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class CollectorError(ValueError):
    """A safe, non-sensitive error suitable for the collector's command line."""


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode_json(payload: str | bytes) -> Any:
    return json.loads(
        payload, parse_constant=_reject_constant, object_pairs_hook=_unique_object
    )


def _read_json(path: Path, description: str) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            raise CollectorError(f"{description} must be an existing regular file.")
        with path.open("rb") as stream:
            payload = stream.read(MAX_JSON_BYTES + 1)
        if len(payload) > MAX_JSON_BYTES:
            raise CollectorError(f"{description} exceeds the size limit.")
        return _decode_json(payload)
    except CollectorError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise CollectorError(f"{description} could not be read as valid JSON.") from None


def normalize_handle(value: Any) -> str:
    if not isinstance(value, str):
        raise CollectorError("A public profile handle is required.")
    normalized = value.removeprefix("@")
    if not _HANDLE.fullmatch(normalized):
        raise CollectorError("The public profile handle is invalid.")
    return normalized


def _email(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 320
        or not re.fullmatch(r"[^@\s]+@[^@\s]+", value)
        or any(ord(character) < 32 for character in value)
    ):
        raise CollectorError("A valid expected account email is required.")
    return value.casefold()


@dataclass(frozen=True)
class AccountSettings:
    account: str
    expected_email: str
    profile_handle: str
    expected_workspace: str | None = None

    def __post_init__(self) -> None:
        if self.account not in {"personal", "work"}:
            raise CollectorError("The account must be personal or work.")
        _email(self.expected_email)
        normalize_handle(self.profile_handle)
        if self.account == "work" and (
            not isinstance(self.expected_workspace, str)
            or not self.expected_workspace.strip()
            or len(self.expected_workspace) > 256
            or any(ord(character) < 32 for character in self.expected_workspace)
        ):
            raise CollectorError("Work collection requires an expected workspace name.")
        if self.account == "personal" and self.expected_workspace is not None:
            raise CollectorError("Personal collection must not specify a work workspace.")


def child_environment(
    settings: AccountSettings, environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Prepare child-only credentials without changing the parent environment."""
    inherited = os.environ if environment is None else environment
    if settings.account == "personal" and "CODEX_ACCESS_TOKEN" in inherited:
        raise CollectorError(
            "Personal collection refuses inherited CODEX_ACCESS_TOKEN; use the managed personal login."
        )
    if "OPENAI_FEDERATION_RULE_ID" in inherited or "OPENAI_IDENTITY_TOKEN_FILE" in inherited:
        raise CollectorError("Workload identity is not supported by this account collector.")
    child = dict(inherited)
    work_token = child.pop("CHATGPT_WORK_ACCESS_TOKEN", None)
    # Platform keys cannot provide these profile statistics and must not select
    # an unrelated authentication path in the child process.
    child.pop("CODEX_API_KEY", None)
    child.pop("OPENAI_API_KEY", None)
    if settings.account == "work":
        if not work_token or not work_token.strip():
            raise CollectorError("Work collection requires CHATGPT_WORK_ACCESS_TOKEN.")
        child["CODEX_ACCESS_TOKEN"] = work_token
    return child


class JsonlAppServer:
    """Small bounded JSONL RPC transport; upstream messages are never logged."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout: float = 20.0,
        on_notification: Callable[[str, Any], None] | None = None,
    ) -> None:
        if not command or not math.isfinite(timeout) or timeout <= 0:
            raise CollectorError("A valid app-server command and timeout are required.")
        self.command = list(command)
        self.environment = dict(environment)
        self.timeout = timeout
        self.on_notification = on_notification
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._messages: queue.Queue[Any] = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._overflow = threading.Event()
        self._next_id = 1

    def __enter__(self) -> "JsonlAppServer":
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.environment,
                shell=False,
                bufsize=0,
            )
        except (OSError, ValueError):
            raise CollectorError("Could not start the local Codex app-server.") from None
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._reader.start()
        return self

    def _enqueue(self, message: Any) -> None:
        try:
            self._messages.put(message, timeout=0.1)
        except queue.Full:
            self._overflow.set()
            self._stop.set()

    def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while not self._stop.is_set():
                line = self._process.stdout.readline(MAX_JSON_BYTES + 1)
                if not line:
                    self._enqueue(_EOF)
                    return
                if len(line) > MAX_JSON_BYTES:
                    raise CollectorError("App-server exceeded the protocol message size limit.")
                if not line.strip():
                    continue
                try:
                    message = _decode_json(line)
                except (ValueError, UnicodeError):
                    raise CollectorError("App-server returned malformed JSON.") from None
                if not isinstance(message, dict):
                    raise CollectorError("App-server returned an invalid protocol message.")
                self._enqueue(message)
        except CollectorError as error:
            self._enqueue(error)
        except (OSError, ValueError):
            if not self._stop.is_set():
                self._enqueue(CollectorError("Could not read the app-server response."))

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise CollectorError("The app-server transport is not running.")
        try:
            self._process.stdin.write(
                (json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n").encode()
            )
            self._process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise CollectorError("Could not send the app-server request.") from None

    def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: Any = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            if self._overflow.is_set():
                raise CollectorError("App-server exceeded the protocol message queue limit.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectorError("App-server response timed out.")
            try:
                response = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise CollectorError("App-server response timed out.") from None
            if response is _EOF:
                raise CollectorError("App-server exited before completing the request.")
            if isinstance(response, CollectorError):
                raise response
            if "method" in response:
                if "id" in response:
                    # External-token refresh, attestations, and other interactive
                    # requests are outside this read-only collector's contract.
                    raise CollectorError("App-server requested an unsupported interactive operation.")
                if not isinstance(response["method"], str):
                    raise CollectorError("App-server returned an invalid notification.")
                if self.on_notification:
                    self.on_notification(response["method"], response.get("params"))
                continue
            if type(response.get("id")) is not int or response["id"] != request_id:
                raise CollectorError("App-server returned an unexpected response identifier.")
            if ("result" in response) == ("error" in response):
                raise CollectorError("App-server returned an invalid RPC response.")
            if "error" in response:
                # Never forward an upstream error message or data. Either may
                # contain identity or authentication details.
                raise CollectorError("App-server rejected the requested operation.")
            return response["result"]

    def __exit__(self, *_exception: Any) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if self._reader is not None:
            self._reader.join(timeout=0.5)
        if process.stdout is not None:
            process.stdout.close()
        self._process = None


def verify_identity(response: Any, settings: AccountSettings) -> None:
    if not isinstance(response, dict) or not isinstance(response.get("account"), dict):
        raise CollectorError("App-server did not return a verifiable signed-in account.")
    account = response["account"]
    if account.get("type") != "chatgpt":
        raise CollectorError("These statistics require a verified ChatGPT account.")
    if account.get("email") is None:
        raise CollectorError(
            "App-server did not expose an account email; identity cannot be verified."
        )
    try:
        matches = _email(account["email"]) == _email(settings.expected_email)
    except (CollectorError, KeyError):
        matches = False
    if not matches:
        raise CollectorError("The signed-in account does not match the expected email.")
    allowed_plans = CONSUMER_PLANS if settings.account == "personal" else WORK_PLANS
    if not isinstance(account.get("planType"), str) or account["planType"] not in allowed_plans:
        raise CollectorError("The signed-in plan does not match the configured account category.")
    if settings.account == "work":
        workspace = account.get("workspaceName")
        if not isinstance(workspace, str) or workspace != settings.expected_workspace:
            raise CollectorError(
                "The work workspace name is missing or does not match the expected workspace."
            )


def sanitize_summary(response: Any) -> dict[str, int | None]:
    if not isinstance(response, dict) or not isinstance(response.get("summary"), dict):
        raise CollectorError("App-server did not return a token-activity summary.")
    summary: dict[str, int | None] = {}
    for field in SUMMARY_FIELDS:
        value = response["summary"].get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_INT64):
            raise CollectorError("A usage metric is not a non-negative integer or null.")
        summary[field] = value
    if all(value is None for value in summary.values()):
        raise CollectorError("All usage metrics are unavailable; existing statistics were retained.")
    if (
        summary["currentStreakDays"] is not None
        and summary["longestStreakDays"] is not None
        and summary["currentStreakDays"] > summary["longestStreakDays"]
    ):
        raise CollectorError("App-server returned inconsistent streak metrics.")
    if (
        summary["peakDailyTokens"] is not None
        and summary["lifetimeTokens"] is not None
        and summary["peakDailyTokens"] > summary["lifetimeTokens"]
    ):
        raise CollectorError("App-server returned inconsistent token metrics.")
    return summary


def _validated_public_data(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    data = _read_json(path, "Public statistics data")
    try:
        validate_data(data, now=now)
    except ValueError:
        raise CollectorError("Existing public statistics data failed schema validation.") from None
    return data


def verify_profile_mapping(data: dict[str, Any], settings: AccountSettings) -> None:
    configured = normalize_handle(settings.profile_handle).casefold()
    existing = normalize_handle(data["accounts"][settings.account]["profileHandle"]).casefold()
    other_account = "work" if settings.account == "personal" else "personal"
    other = normalize_handle(data["accounts"][other_account]["profileHandle"]).casefold()
    if configured != existing:
        raise CollectorError("The configured profile handle does not match its existing account mapping.")
    if configured == other:
        raise CollectorError("Personal and work must not map to the same public profile handle.")


@contextmanager
def _file_lock(path: Path, timeout: float = 5.0) -> Iterator[None]:
    """Portable single-writer lock; never guess whether an abandoned lock is safe."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CollectorError(
                    "Statistics data is locked; check for another collector or an abandoned lock."
                ) from None
            time.sleep(0.05)
        except OSError:
            raise CollectorError("Could not lock the statistics data file.") from None
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def atomic_update(
    path: Path,
    settings: AccountSettings,
    entry: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    temporary: str | None = None
    with _file_lock(path):
        # Re-read under the lock so simultaneous account collectors preserve
        # one another's independently timestamped updates.
        data = _validated_public_data(path, now=now)
        verify_profile_mapping(data, settings)
        data["accounts"][settings.account] = entry
        try:
            validate_data(data, now=now)
        except ValueError:
            raise CollectorError("Collected statistics failed public schema validation.") from None
        try:
            payload = json.dumps(data, indent=2, allow_nan=False, ensure_ascii=True) + "\n"
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except (OSError, ValueError):
            raise CollectorError("Could not atomically save the statistics data file.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def collect(
    settings: AccountSettings,
    data_path: Path,
    *,
    codex: str = "codex",
    timeout: float = 20.0,
    environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
    transport_factory: Callable[..., Any] = JsonlAppServer,
) -> dict[str, Any]:
    """Collect one account, preserving the other account and all private identity data."""
    data_path = Path(data_path)
    verify_profile_mapping(_validated_public_data(data_path, now=now), settings)
    child = child_environment(settings, environment)
    command = [codex, "app-server", "--listen", "stdio://"]
    if settings.account == "work":
        # A missing or unsuitable token must not fall back to personal cached
        # credentials. Admin-enforced restrictions remain in force.
        command.extend(["-c", 'cli_auth_credentials_store="ephemeral"'])
    identity_verified = False

    def on_notification(method: str, params: Any) -> None:
        if method != "account/updated":
            return
        if identity_verified:
            raise CollectorError("Authentication changed during collection; no update was saved.")
        modes = {"chatgpt"} if settings.account == "personal" else {"agentIdentity", "personalAccessToken"}
        if (
            not isinstance(params, dict)
            or not isinstance(params.get("authMode"), str)
            or params["authMode"] not in modes
        ):
            raise CollectorError("The authentication mode does not match the configured account.")

    with transport_factory(
        command, environment=child, timeout=timeout, on_notification=on_notification
    ) as transport:
        initialized = transport.request(
            "initialize",
            {"clientInfo": {"name": "hendoctor_profile_stats", "version": "1.0.0"}},
        )
        if not isinstance(initialized, dict):
            raise CollectorError("App-server initialization returned an invalid response.")
        transport.notify("initialized", {})
        verify_identity(transport.request("account/read", {"refreshToken": False}), settings)
        identity_verified = True
        summary = sanitize_summary(transport.request("account/usage/read"))
        # Drain any queued auth notifications and recheck identity after the
        # usage response before publishing its metrics.
        verify_identity(transport.request("account/read", {"refreshToken": False}), settings)

    fetched = now or datetime.now(timezone.utc)
    if fetched.tzinfo is None or fetched.utcoffset() is None:
        raise CollectorError("The collection timestamp must have an explicit timezone.")
    entry = {
        "source": "account/usage/read",
        "fetchedAt": fetched.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "profileHandle": normalize_handle(settings.profile_handle),
        "summary": summary,
    }
    atomic_update(data_path, settings, entry, now=fetched)
    return entry


def settings_from_args(args: argparse.Namespace) -> AccountSettings:
    configured: dict[str, Any] = {}
    if args.config is not None:
        config = _read_json(Path(args.config), "Private identity configuration")
        if not isinstance(config, dict) or set(config) - {"personal", "work"}:
            raise CollectorError("Private identity configuration has invalid account names.")
        for entry in config.values():
            if not isinstance(entry, dict) or set(entry) - {
                "expectedEmail", "profileHandle", "expectedWorkspace"
            }:
                raise CollectorError("Private identity configuration has invalid account fields.")
        configured = config.get(args.account, {})

    def setting(name: str, supplied: Any) -> Any:
        configured_value = configured.get(name)
        if supplied is not None and configured_value is not None and supplied != configured_value:
            raise CollectorError("Command-line identity settings conflict with private configuration.")
        return supplied if supplied is not None else configured_value

    return AccountSettings(
        account=args.account,
        expected_email=setting("expectedEmail", args.expected_email),
        profile_handle=setting("profileHandle", args.profile_handle),
        expected_workspace=setting("expectedWorkspace", args.expected_workspace),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=("personal", "work"))
    parser.add_argument("--data", required=True, type=Path, help="Existing public statistics JSON file")
    parser.add_argument("--expected-email", help="Expected private account identity, never published")
    parser.add_argument("--profile-handle", help="Public profile handle already mapped in the data file")
    parser.add_argument("--expected-workspace", help="Exact workspace display name, required for work")
    parser.add_argument("--config", type=Path, help="Optional private JSON identity configuration")
    parser.add_argument("--codex", default="codex", help="Installed Codex executable")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout for each RPC, in seconds")
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
            raise CollectorError("The RPC timeout must be between zero and 120 seconds.")
        settings = settings_from_args(args)
        collect(settings, args.data, codex=args.codex, timeout=args.timeout)
    except CollectorError as error:
        print(f"ChatGPT statistics update failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ChatGPT statistics update interrupted.", file=sys.stderr)
        return 130
    except Exception:
        # Keep unexpected third-party/runtime messages out of public job logs.
        print("ChatGPT statistics update failed unexpectedly; inspect the private runner.", file=sys.stderr)
        return 1
    print(f"Updated {args.account} statistics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
