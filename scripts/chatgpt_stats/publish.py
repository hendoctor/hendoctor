#!/usr/bin/env python3
"""Publish validated, aggregate-only ChatGPT usage without handling account auth.

Run the collector privately. This publisher uses ``gh`` authentication and reads
the current README from GitHub so an automated refresh cannot erase unrelated
README edits. It never downloads or runs remote executable code.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote

try:
    from . import render
except ImportError:
    import render  # type: ignore[no-redef]


README_PATH = "README.md"
DATA_PATH = "data/chatgpt-usage.json"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_README_BYTES = 2 * 1024 * 1024
MAX_DATA_BYTES = 64 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class PublishError(RuntimeError):
    """An operational error whose message is safe to display publicly."""


class GitHubClient:
    def __init__(self, *, executable: str = "gh", timeout: float = 30) -> None:
        if not 0 < timeout <= 60:
            raise PublishError("The GitHub timeout must be between 0 and 60 seconds.")
        self.executable = executable
        self.timeout = timeout

    def request(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        """Call only the specified GitHub endpoint; do not echo raw CLI output."""
        args = [self.executable, "api", "--hostname", "github.com", "--method", method,
                "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: 2026-03-10", endpoint]
        stdin = None
        if payload is not None:
            args.extend(["--input", "-"])
            stdin = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        try:
            result = subprocess.run(args, input=stdin, text=True, capture_output=True,
                                    timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise PublishError("GitHub request timed out. Check the remote branch before retrying.") from exc
        except OSError as exc:
            raise PublishError("Cannot run GitHub CLI. Install gh and authenticate on this trusted host.") from exc
        if result.returncode != 0:
            # gh's raw stderr may include authentication or server diagnostics.
            raise PublishError("GitHub request failed or could not be confirmed. Check authentication, permissions, and the remote branch before retrying.")
        if len(result.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise PublishError("GitHub response exceeded the size limit.")
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise PublishError("GitHub returned an invalid JSON response.") from exc
        if not isinstance(value, dict):
            raise PublishError("GitHub returned an unexpected response shape.")
        return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise PublishError("GitHub returned an invalid commit or tree identifier.")
    return value


def _nested_sha(response: dict, field: str) -> str:
    value = response.get(field)
    return _sha(value.get("sha") if isinstance(value, dict) else None)


def _target(repository: str, branch: str) -> tuple[str, str]:
    if not REPO_RE.fullmatch(repository):
        raise PublishError("Repository must be an owner/name pair.")
    if (not branch or branch.startswith(("/", ".")) or branch.endswith(("/", "."))
            or any(char in branch for char in " ~^:?*[\\")
            or ".." in branch or "//" in branch or "@{" in branch
            or any(ord(char) < 32 or ord(char) == 127 for char in branch)
            or any(part.endswith(".lock") or part.startswith(".") for part in branch.split("/"))):
        raise PublishError("Branch name is not a valid Git ref.")
    return f"repos/{repository}", quote(branch, safe="")


def _read_file(client: GitHubClient, prefix: str, path: str, ref: str, limit: int) -> str:
    response = client.request("GET", f"{prefix}/contents/{path}?ref={ref}")
    if response.get("type") != "file" or response.get("encoding") != "base64":
        raise PublishError("Expected repository data file is missing or unsupported.")
    encoded = response.get("content")
    if not isinstance(encoded, str) or len(encoded) > limit * 2:
        raise PublishError("Repository data file exceeded its size limit.")
    try:
        content = base64.b64decode("".join(encoded.split()), validate=True)
        if len(content) > limit:
            raise PublishError("Repository data file exceeded its size limit.")
        return content.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise PublishError("Repository data file has invalid encoding.") from exc


def _timestamp(value: str) -> datetime:
    # render.validate_data has already enforced a UTC, timezone-aware timestamp.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def merge_data(remote: dict, incoming: dict, *, now: datetime | None = None) -> dict:
    """Merge newer snapshots per identity, refusing conflicts or provenance loss."""
    render.validate_data(remote, now=now)
    render.validate_data(incoming, now=now)
    merged = copy.deepcopy(remote)
    for name in ("personal", "work"):
        existing = remote["accounts"][name]
        candidate = incoming["accounts"][name]
        if candidate["profileHandle"] != existing["profileHandle"]:
            raise PublishError("Incoming stats do not match the configured profile handles.")
        old_time = _timestamp(existing["fetchedAt"])
        new_time = _timestamp(candidate["fetchedAt"])
        if new_time < old_time:
            continue
        if new_time == old_time:
            if candidate != existing:
                raise PublishError("Two different snapshots have the same collection timestamp.")
            continue
        if existing["source"] == "account/usage/read" and candidate["source"] != "account/usage/read":
            raise PublishError("A manual screenshot cannot replace an authenticated usage snapshot.")
        merged["accounts"][name] = copy.deepcopy(candidate)
    render.validate_data(merged, now=now)
    return merged


@dataclass(frozen=True)
class PublishPlan:
    repository: str
    branch: str
    parent_sha: str
    base_tree_sha: str
    old_readme: str
    new_readme: str
    old_data: str
    new_data: str

    @property
    def changed_files(self) -> dict[str, str]:
        files = {}
        if self.new_readme != self.old_readme:
            files[README_PATH] = self.new_readme
        if self.new_data != self.old_data:
            files[DATA_PATH] = self.new_data
        return files


def prepare_plan(client: GitHubClient, *, repository: str = "hendoctor/hendoctor",
                 branch: str = "main", incoming: dict | None = None,
                 now: datetime | None = None) -> PublishPlan:
    """Read a consistent remote snapshot and compute changes without writing."""
    prefix, encoded_branch = _target(repository, branch)
    now = now or datetime.now(timezone.utc)
    if incoming is not None:
        render.validate_data(incoming, now=now)
    ref = client.request("GET", f"{prefix}/git/ref/heads/{encoded_branch}")
    parent_sha = _nested_sha(ref, "object")
    commit = client.request("GET", f"{prefix}/git/commits/{parent_sha}")
    base_tree = _nested_sha(commit, "tree")
    # Both contents calls are pinned to the captured SHA, never a moving branch.
    old_readme = _read_file(client, prefix, README_PATH, parent_sha, MAX_README_BYTES)
    old_data = _read_file(client, prefix, DATA_PATH, parent_sha, MAX_DATA_BYTES)
    try:
        remote = json.loads(old_data)
    except ValueError as exc:
        raise PublishError("The remote stats file is not valid JSON.") from exc
    render.validate_data(remote, now=now)
    data = merge_data(remote, incoming, now=now) if incoming is not None else remote
    new_readme = render.update_readme(old_readme, data, now=now)
    new_data = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    return PublishPlan(repository, branch, parent_sha, base_tree,
                       old_readme, new_readme, old_data, new_data)


def publish_plan(client: GitHubClient, plan: PublishPlan) -> str | None:
    """Write only the planned files. A concurrent branch advance fails safely."""
    if not plan.changed_files:
        return None
    prefix, encoded_branch = _target(plan.repository, plan.branch)
    tree_response = client.request("POST", f"{prefix}/git/trees", {
        "base_tree": plan.base_tree_sha,
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": content}
            for path, content in plan.changed_files.items()
        ],
    })
    tree_sha = _sha(tree_response.get("sha"))
    commit_response = client.request("POST", f"{prefix}/git/commits", {
        "message": "Refresh ChatGPT profile usage",
        "tree": tree_sha,
        "parents": [plan.parent_sha],
    })
    commit_sha = _sha(commit_response.get("sha"))
    # GitHub rejects a non-fast-forward ref update. Never force or automatically
    # rebase/retry a stale plan, so a concurrent README edit cannot be lost.
    ref_response = client.request("PATCH", f"{prefix}/git/refs/heads/{encoded_branch}", {
        "sha": commit_sha,
        "force": False,
    })
    if _nested_sha(ref_response, "object") != commit_sha:
        raise PublishError("GitHub did not confirm the expected branch update. Inspect the remote branch before retrying.")
    return commit_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", type=Path, help="Validated aggregate JSON produced by the private collector")
    source.add_argument("--refresh-only", action="store_true", help="Re-render remote stats for freshness without collecting usage")
    parser.add_argument("--repository", "--repo", dest="repository", default="hendoctor/hendoctor")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--publish", action="store_true", help="Commit changes; otherwise perform a read-only dry run")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        incoming = None
        if args.data is not None:
            if args.data.stat().st_size > MAX_DATA_BYTES:
                raise PublishError("Local stats file exceeded its size limit.")
            incoming = json.loads(args.data.read_text(encoding="utf-8"))
        client = GitHubClient(timeout=args.timeout)
        plan = prepare_plan(client, repository=args.repository, branch=args.branch, incoming=incoming)
    except PublishError as exc:
        print(f"Publish stopped: {exc}", file=sys.stderr)
        return 1
    except (ValueError, TypeError, KeyError, OSError):
        print("Publish stopped: stats or README validation failed. No GitHub write was attempted.", file=sys.stderr)
        return 1
    result = {"repository": plan.repository, "branch": plan.branch,
              "dryRun": not args.publish, "changedFiles": list(plan.changed_files)}
    if args.publish:
        try:
            sha = publish_plan(client, plan)
        except PublishError as exc:
            print(f"Publish stopped: {exc}", file=sys.stderr)
            return 1
        if sha:
            result["commitUrl"] = f"https://github.com/{plan.repository}/commit/{sha}"
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
