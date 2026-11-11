#!/usr/bin/env python3
"""Validate public ChatGPT stats and replace only their README marker section.

This module performs no network requests. Screenshots remain display-only
snapshots; only account/usage/read metrics are accepted as exact integers.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import quote


START_MARKER = "<!-- CHATGPT_ACTIVITY_START -->"
END_MARKER = "<!-- CHATGPT_ACTIVITY_END -->"
MAX_METRIC = 2**53 - 1
SUMMARY_FIELDS = (
    "lifetimeTokens",
    "peakDailyTokens",
    "longestRunningTurnSec",
    "currentStreakDays",
    "longestStreakDays",
)
ACCOUNT_NAMES = ("personal", "work")
ACCOUNT_FIELDS = {"source", "fetchedAt", "profileHandle", "summary"}
UTC_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)
HANDLE_PATTERN = re.compile(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
TOKEN_DISPLAY_PATTERN = re.compile(r"(?:0|[1-9]\d{0,14})(?:\.\d{1,6})?[KMBT]?\Z")
DAYS_DISPLAY_PATTERN = re.compile(r"(?:0|[1-9]\d{0,14}) days?\Z")
DURATION_DISPLAY_PATTERN = re.compile(
    r"(?:\d{1,12}d(?: \d{1,2}h)?(?: \d{1,2}m)?(?: \d{1,2}s)?"
    r"|\d{1,12}h(?: \d{1,2}m)?(?: \d{1,2}s)?"
    r"|\d{1,12}m(?: \d{1,2}s)?|\d{1,12}s)\Z"
)


class ValidationError(ValueError):
    """Input cannot safely be represented in the public README."""


def _require_keys(value: Any, expected: set[str], path: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")
    if set(value) != expected:
        # Never echo unexpected field names or input values into public logs.
        raise ValidationError(f"{path} has missing or unexpected fields")


def _parse_utc(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not UTC_PATTERN.fullmatch(value):
        raise ValidationError(f"{path} must be an ISO 8601 UTC timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{path} must be a valid UTC timestamp") from None


def _resolve_now(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        return _parse_utc(now, "now")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValidationError("now must be a timezone-aware datetime or UTC timestamp")
    return now.astimezone(timezone.utc)


def validate_data(
    data: Any, *, now: datetime | str | None = None
) -> dict[str, Any]:
    """Validate schema v1 without modifying it; return the original mapping.

    Both named accounts and all five summary keys are required. Screenshot
    accounts require null summaries and a separate snapshotDisplay object.
    API accounts must omit snapshotDisplay. Errors never include raw values.
    """
    observed_now = _resolve_now(now)
    _require_keys(data, {"schemaVersion", "accounts"}, "document")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        raise ValidationError("schemaVersion must be 1")
    _require_keys(data["accounts"], set(ACCOUNT_NAMES), "accounts")
    for name in ACCOUNT_NAMES:
        account = data["accounts"][name]
        path = f"accounts.{name}"
        if not isinstance(account, dict):
            raise ValidationError(f"{path} must be an object")
        source = account.get("source")
        if source not in ("screenshot", "account/usage/read"):
            raise ValidationError(f"{path}.source is unsupported")
        expected = ACCOUNT_FIELDS | ({"snapshotDisplay"} if source == "screenshot" else set())
        _require_keys(account, expected, path)
        fetched_at = _parse_utc(account["fetchedAt"], f"{path}.fetchedAt")
        if fetched_at - observed_now > timedelta(minutes=5):
            raise ValidationError(f"{path}.fetchedAt is more than five minutes in the future")
        handle = account["profileHandle"]
        if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
            raise ValidationError(f"{path}.profileHandle is invalid")
        _require_keys(account["summary"], set(SUMMARY_FIELDS), f"{path}.summary")
        for key in SUMMARY_FIELDS:
            value = account["summary"][key]
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_METRIC):
                raise ValidationError(f"{path}.summary.{key} must be a safe nonnegative integer or null")
            if source == "screenshot" and value is not None:
                raise ValidationError(f"{path}.summary must contain only nulls for a screenshot")
        if source == "screenshot":
            display = account["snapshotDisplay"]
            _require_keys(display, set(SUMMARY_FIELDS), f"{path}.snapshotDisplay")
            for key in SUMMARY_FIELDS:
                pattern = (
                    TOKEN_DISPLAY_PATTERN if key in ("lifetimeTokens", "peakDailyTokens")
                    else DURATION_DISPLAY_PATTERN if key == "longestRunningTurnSec"
                    else DAYS_DISPLAY_PATTERN
                )
                if not isinstance(display[key], str) or not pattern.fullmatch(display[key]):
                    raise ValidationError(f"{path}.snapshotDisplay.{key} has an invalid display value")
    return data


def _duration(seconds: int) -> str:
    parts = []
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{suffix}")
    return " ".join(parts) or "0s"


def _metric(account: dict[str, Any], key: str) -> str:
    if account["source"] == "screenshot":
        return account["snapshotDisplay"][key]
    value = account["summary"][key]
    if value is None:
        return "Unavailable"
    if key == "longestRunningTurnSec":
        return _duration(value)
    if key in ("currentStreakDays", "longestStreakDays"):
        return f"{value:,} day" + ("" if value == 1 else "s")
    return f"{value:,}"


def _profile(account: dict[str, Any]) -> str:
    handle = "@" + account["profileHandle"].lstrip("@")
    # Preserve the existing verified personal link; do not guess other URLs.
    if handle == "@jmhenn_":
        return f"[{handle}](https://chatgpt.com/u/jmhenn_)"
    return f"`{handle}`"


def render_section(data: Any, *, now: datetime | str | None = None) -> str:
    """Return the stats section without marker comments or a trailing newline."""
    observed_now = _resolve_now(now)
    validate_data(data, now=observed_now)
    accounts = [data["accounts"][name] for name in ACCOUNT_NAMES]
    lines = ["## ChatGPT activity", ""]
    for name, color, account in zip(ACCOUNT_NAMES, ("166534", "0078D4"), accounts):
        label = name.title()
        value = _metric(account, "lifetimeTokens")
        message = quote(f"{value}_lifetime_tokens", safe="_.")
        lines.append(
            f"![{label} ChatGPT lifetime tokens: {value}]"
            f"(https://img.shields.io/badge/{label}_ChatGPT-{message}-{color}?style=for-the-badge)"
        )
    lines += ["", "| Metric | Personal | Work |", "| --- | --- | --- |"]
    rows = [("Profile", [_profile(account) for account in accounts])]
    for label, key in (
        ("Lifetime tokens", "lifetimeTokens"),
        ("Peak tokens", "peakDailyTokens"),
        ("Longest task", "longestRunningTurnSec"),
        ("Longest streak", "longestStreakDays"),
        ("Streak at last sync", "currentStreakDays"),
    ):
        rows.append((label, [_metric(account, key) for account in accounts]))
    rows.append(("Source", [
        "Manual screenshot snapshot" if account["source"] == "screenshot"
        else "ChatGPT account usage" for account in accounts
    ]))
    timestamps = [_parse_utc(account["fetchedAt"], "fetchedAt") for account in accounts]
    rows.append(("Last recorded", [
        timestamp.strftime("%Y-%m-%d %H:%M UTC")
        for timestamp in timestamps
    ]))
    rows.append(("Freshness", [
        "**Stale (>48h)**" if observed_now - timestamp > timedelta(hours=48)
        else "Recorded within 48h" for timestamp in timestamps
    ]))
    lines.extend(f"| {label} | {values[0]} | {values[1]} |" for label, values in rows)
    if any(account["source"] == "screenshot" for account in accounts):
        footer = (
            "Screenshot values are manually transcribed, rounded display values. "
            "Account usage values reflect their latest successful refresh; "
            "missing metrics are unavailable."
        )
    else:
        footer = (
            "Values reflect the latest successful account refresh; "
            "missing metrics are unavailable."
        )
    footer += " Streaks reflect the last recorded sync."
    lines += ["", f"*{footer}*"]
    return "\n".join(lines)


def update_readme(
    readme_text: str, data: Any, *, now: datetime | str | None = None
) -> str:
    """Replace one standalone marker pair's contents, preserving all other text."""
    for marker in (START_MARKER, END_MARKER):
        if readme_text.count(marker) != 1 or not re.search(
            rf"(?m)^{re.escape(marker)}\r?$", readme_text
        ):
            raise ValidationError("README must contain exactly one standalone start and end marker")
    start = readme_text.index(START_MARKER) + len(START_MARKER)
    end = readme_text.index(END_MARKER)
    if end < start:
        raise ValidationError("README markers are reversed")
    newline = "\r\n" if readme_text.startswith("\r\n", start) else "\n"
    section = render_section(data, now=now).replace("\n", newline)
    return readme_text[:start] + newline + section + newline + readme_text[end:]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("JSON contains duplicate fields")
        result[key] = value
    return result


def _write_atomic(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content.encode("utf-8"))
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="Exit 1 if the section needs updating; never write")
    parser.add_argument("--now", help="Override current time with an ISO 8601 UTC timestamp")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.data.read_bytes().decode("utf-8"), object_pairs_hook=_unique_object)
        before = args.readme.read_bytes().decode("utf-8")
        after = update_readme(before, data, now=args.now)
        if before == after:
            print("ChatGPT activity section is current.")
            return 0
        if args.check:
            print("ChatGPT activity section needs updating.")
            return 1
        _write_atomic(args.readme, after)
        print("Updated the ChatGPT activity section.")
        return 0
    except ValidationError as exc:
        print(f"Invalid public stats: {exc}", file=sys.stderr)
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("Could not read or write valid UTF-8 README and JSON files.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
