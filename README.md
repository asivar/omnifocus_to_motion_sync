# OmniFocus to Motion Sync Script

**`sync_of_to_motion.py`**  
Synchronize tasks and projects from **OmniFocus** to **Motion** with optional autoscheduling, due dates, priorities, and smart matching.

## 🚀 Overview

This script bridges OmniFocus and Motion to keep your tasks aligned across platforms. It supports syncing folders, projects, and tasks with detailed conditions, handling task creation and deletion intelligently. You can run in `dry_run` mode to preview actions without changes, or fully synchronize in `sync` mode.

## 🔧 Features

- Fetches folders, projects, and tasks from OmniFocus.
- Matches them to corresponding workspaces and projects in Motion.
- Adds new tasks from OmniFocus to Motion, respecting due dates, durations, and flags.
- Deletes completed tasks in Motion if marked complete in OmniFocus.
- Maps workspaces to Motion schedules using `WORKSPACE_TO_SCHEDULE_MAP`.
- Supports different sync modes: `dry_run`, `sync`, and `strict`.
- Respects Motion API rate limits (12 req/min).

## 🛠️ Requirements

- macOS with OmniFocus installed and running
- Python 3.7+
- Motion API Key (get it from your Motion account)
- Internet connection (for Motion API access)

## 🔐 Environment Variables

- `MOTION_API_KEY` — *(required)* your Motion API key.
- `OMNIFOCUS_SYNC_MODE` — *(optional)* one of:
  - `dry_run` (default): simulate sync without changes
  - `sync`: perform synchronization
  - `strict`: like `sync`, but only exact name matches are used
- `OMNIFOCUS_SKIP_FOLDERS` — *(optional)* comma-separated names of OmniFocus folders to skip.

## 📅 Workspace → Schedule Mapping

Update the following dict to map workspaces to Motion schedule names:

```python
WORKSPACE_TO_SCHEDULE_MAP = {
    "🏠 Home": "Personal hours",
    "Work": "Work horus",
}
```

## ▶️ Usage

```bash
# Dry run (default)
python3 sync_of_to_motion.py

# Full sync (creates/deletes items)
OMNIFOCUS_SYNC_MODE=sync python3 sync_of_to_motion.py

# Strict sync (exact name matches only)
OMNIFOCUS_SYNC_MODE=strict python3 sync_of_to_motion.py
```

## 💾 Sync State Tracking

The script saves the last successful sync time to `sync_of_motion_state.json`. Only new or modified OmniFocus items are processed unless no state file exists.

## 📈 Logging

The script prints detailed status updates, including:
- Items created, deleted, or skipped
- API rate limiting status
- Schedule mapping and errors

## ⚠️ Notes

- Ensure OmniFocus is open when running the script.
- This script does **not** sync data from Motion back to OmniFocus.
- Deleted Motion tasks are based on completed OmniFocus tasks only.

## 📂 File Structure

- `sync_of_to_motion.py` – main synchronization script
- `sync_of_motion_state.json` – state file for tracking last sync time (auto-generated)
