#!/usr/bin/env python3
"""Run the private collectors serially, then optionally publish safe aggregates."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys


@contextlib.contextmanager
def exclusive_run(data_path: Path):
    """An OS lock is released even if the scheduler terminates this process."""
    lock_path = data_path.with_suffix(data_path.suffix + ".run.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            if not lock.read(1):
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another stats refresh is already running.") from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another stats refresh is already running.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_step(script: Path, args: list[str], *, env=None) -> bool:
    # The called programs emit only bounded, sanitized status/error messages.
    try:
        result = subprocess.run(
            [sys.executable, str(script), *args],
            env=env,
            check=False,
            timeout=240,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"{script.stem}: timed out. Publication may be incomplete; inspect the GitHub branch before retrying.", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private identity configuration.")
    parser.add_argument("--data", type=Path, required=True, help="Private working copy of the sanitized JSON.")
    parser.add_argument("--repo", default="hendoctor/hendoctor")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--publish", action="store_true", help="Write refreshed metrics to GitHub.")
    args = parser.parse_args()
    scripts = Path(__file__).resolve().parent

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not config or set(config) - {"personal", "work"}:
            raise ValueError("Private config must contain personal and/or work account sections.")
        accounts = [account for account in ("personal", "work") if account in config]
        failures = False
        with exclusive_run(args.data):
            for account in accounts:
                print(f"Refreshing {account} usage.", flush=True)
                ok = run_step(scripts / "collect.py", [
                    "--account", account,
                    "--config", str(args.config.resolve()),
                    "--data", str(args.data.resolve()),
                ])
                failures = failures or not ok
            # The publisher receives no work token and reads only public aggregates.
            publish_env = dict(os.environ)
            publish_env.pop("CHATGPT_WORK_ACCESS_TOKEN", None)
            publish_env.pop("CODEX_ACCESS_TOKEN", None)
            publish_args = [
                "--data", str(args.data.resolve()),
                "--repo", args.repo,
                "--branch", args.branch,
            ]
            if args.publish:
                publish_args.append("--publish")
            ok = run_step(scripts / "publish.py", publish_args, env=publish_env)
            failures = failures or not ok
        if failures:
            print("Refresh incomplete. Failed accounts retain their prior snapshot and timestamp.", file=sys.stderr)
            return 1
        return 0
    except (OSError, ValueError, RuntimeError):
        print("Refresh could not start. Check the private config, data path, and concurrent-run lock.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
