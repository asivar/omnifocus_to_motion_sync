#!/usr/bin/env python3
"""
OmniFocus-Motion Sync Health Check

Monitors the sync system and sends macOS notifications on issues:
- Stale sync (last sync > 2 hours ago)
- Stale lock file (> 10 minutes old)
- Excessive errors in log file (> 10 in last hour)

Usage: python3 health_check.py [--config config.json]
"""

import os
import sys
import json
import re
import argparse
import subprocess
from datetime import datetime, timedelta, timezone

# Defaults (overridden by config if available)
DEFAULT_STATE_FILE = "motion_hierarchical_mapping.json"
DEFAULT_LOCK_FILE = "/tmp/of2motion.lock"
DEFAULT_LOG_DIR = os.path.expanduser("~/Library/Logs/OmniFocusMotionSync")
DEFAULT_LOG_FILE = os.path.join(DEFAULT_LOG_DIR, "sync.log")

STALE_SYNC_THRESHOLD_HOURS = 2
STALE_LOCK_THRESHOLD_MINUTES = 10
ERROR_THRESHOLD_PER_HOUR = 10


def send_notification(title: str, message: str):
    """Send a macOS notification via osascript."""
    escaped_title = title.replace('"', '\\"')
    escaped_message = message.replace('"', '\\"')
    script = f'display notification "{escaped_message}" with title "{escaped_title}"'
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception:
        # Fall back to printing if notification fails
        print(f"[ALERT] {title}: {message}")


def check_sync_staleness(state_file: str) -> bool:
    """Check if the last sync is too old. Returns True if healthy."""
    if not os.path.exists(state_file):
        send_notification(
            "Sync Health: No State File",
            f"State file not found: {state_file}"
        )
        return False

    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        last_sync = data.get('metadata', {}).get('last_sync_timestamp')
        if not last_sync:
            send_notification(
                "Sync Health: No Timestamp",
                "State file has no last_sync_timestamp"
            )
            return False

        last_sync_dt = datetime.fromisoformat(last_sync.replace('Z', '+00:00'))
        age = datetime.now(timezone.utc) - last_sync_dt
        age_hours = age.total_seconds() / 3600

        if age_hours > STALE_SYNC_THRESHOLD_HOURS:
            send_notification(
                "Sync Health: Stale Sync",
                f"Last sync was {age_hours:.1f} hours ago (threshold: {STALE_SYNC_THRESHOLD_HOURS}h)"
            )
            return False

        print(f"OK: Last sync {age_hours:.1f} hours ago")
        return True

    except (json.JSONDecodeError, IOError, ValueError) as e:
        send_notification(
            "Sync Health: State File Error",
            f"Could not read state file: {e}"
        )
        return False


def check_stale_lock(lock_file: str) -> bool:
    """Check for stale lock files. Returns True if healthy."""
    if not os.path.exists(lock_file):
        print("OK: No lock file present")
        return True

    try:
        lock_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(lock_file))
        age_minutes = lock_age.total_seconds() / 60

        if age_minutes > STALE_LOCK_THRESHOLD_MINUTES:
            send_notification(
                "Sync Health: Stale Lock",
                f"Lock file is {age_minutes:.0f} min old (threshold: {STALE_LOCK_THRESHOLD_MINUTES} min). "
                f"Sync may be stuck."
            )
            return False

        print(f"OK: Lock file is {age_minutes:.0f} min old (active sync)")
        return True

    except OSError as e:
        send_notification(
            "Sync Health: Lock Check Error",
            f"Could not check lock file: {e}"
        )
        return False


def check_log_errors(log_file: str) -> bool:
    """Check for excessive errors in the last hour. Returns True if healthy."""
    if not os.path.exists(log_file):
        print("OK: No log file found (first run?)")
        return True

    one_hour_ago = datetime.now() - timedelta(hours=1)
    error_count = 0
    error_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                if '[ERROR]' not in line:
                    continue

                match = error_pattern.match(line)
                if not match:
                    continue

                try:
                    log_time = datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S')
                    if log_time > one_hour_ago:
                        error_count += 1
                except ValueError:
                    continue

        if error_count > ERROR_THRESHOLD_PER_HOUR:
            send_notification(
                "Sync Health: Excessive Errors",
                f"{error_count} errors in the last hour (threshold: {ERROR_THRESHOLD_PER_HOUR})"
            )
            return False

        print(f"OK: {error_count} errors in last hour")
        return True

    except IOError as e:
        send_notification(
            "Sync Health: Log Check Error",
            f"Could not read log file: {e}"
        )
        return False


def load_config(config_file: str) -> dict:
    """Load config file for path settings."""
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def main():
    parser = argparse.ArgumentParser(description="OmniFocus-Motion Sync Health Check")
    parser.add_argument("--config", type=str, default="config.json",
                        help="Path to configuration file")
    args = parser.parse_args()

    config = load_config(args.config)
    paths = config.get("paths", {})

    state_file = paths.get("state_file", DEFAULT_STATE_FILE)
    lock_file = paths.get("lock_file", DEFAULT_LOCK_FILE)
    log_dir = os.path.expanduser(paths.get("log_directory", DEFAULT_LOG_DIR))
    log_file = os.path.join(log_dir, "sync.log")

    print(f"Running health check at {datetime.now().isoformat()}")
    print(f"  State file: {state_file}")
    print(f"  Lock file: {lock_file}")
    print(f"  Log file: {log_file}")
    print()

    results = []
    results.append(("Sync staleness", check_sync_staleness(state_file)))
    results.append(("Stale lock", check_stale_lock(lock_file)))
    results.append(("Log errors", check_log_errors(log_file)))

    print()
    all_healthy = all(ok for _, ok in results)
    if all_healthy:
        print("All checks passed.")
    else:
        failed = [name for name, ok in results if not ok]
        print(f"FAILED checks: {', '.join(failed)}")

    sys.exit(0 if all_healthy else 1)


if __name__ == "__main__":
    main()
