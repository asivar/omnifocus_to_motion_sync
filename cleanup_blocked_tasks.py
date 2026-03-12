#!/usr/bin/env python3
"""
One-time cleanup: Remove non-first-action tasks from Motion for sequential projects.

With next_action_only mode enabled, only the first incomplete task in each sequential
project should exist in Motion. This script removes the rest.

Usage:
    python3 cleanup_blocked_tasks.py              # Dry run (default)
    python3 cleanup_blocked_tasks.py --execute    # Actually delete tasks

What it does:
1. Reads OmniFocus to find sequential projects and their task order
2. Reads the Motion mapping file to find which tasks are synced
3. For each sequential project, identifies tasks that are NOT the first incomplete
4. Deletes those tasks from Motion via API
5. Removes them from the mapping file
"""

import os
import sys
import json
import time
import argparse

# Add script directory to path so we can import from sync script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sync_of_to_motion import (
    Config, MotionSync, MotionHybridSync, NOT_FOUND,
    setup_logging, logger, STATE_FILE
)


def load_mapping(state_file: str) -> dict:
    """Load the Motion hierarchical mapping file."""
    with open(state_file, 'r') as f:
        return json.load(f)


def save_mapping(state_file: str, data: dict):
    """Save the Motion hierarchical mapping file."""
    with open(state_file, 'w') as f:
        json.dump(data, f, indent=2)


def find_tasks_to_remove(of_structure, mapping_data, config):
    """
    For each sequential project, find Motion tasks that are NOT the first
    incomplete task and should be removed under next_action_only mode.

    Returns list of dicts: {of_id, motion_id, task_name, project_name, workspace, mapping_key}
    """
    tasks_to_remove = []
    task_mappings = mapping_data.get("task_mappings", {})
    ignored_folders = config.ignored_folders

    # Build reverse lookup: of_id -> mapping entry
    of_id_to_mapping = {}
    for key, entry in task_mappings.items():
        of_id = entry.get("of_id")
        if of_id:
            of_id_to_mapping[of_id] = {**entry, "mapping_key": key}

    for folder in of_structure:
        folder_name = folder.name.strip()
        if folder_name in ignored_folders:
            continue

        for project in folder.projects:
            if project.completed:
                continue

            project_name = project.name.strip()

            # Build sequential action group map: group_id -> first incomplete child id
            seq_group_next_action = {}
            for t in project.tasks:
                if t.parent_id and t.parent_id not in seq_group_next_action and not t.completed:
                    parent_task = next((pt for pt in project.tasks if pt.id == t.parent_id and pt.is_sequential_group), None)
                    if parent_task:
                        seq_group_next_action[t.parent_id] = t.id

            # For project-level sequential: find first incomplete top-level item
            first_top_level_id = None
            if project.sequential:
                for t in project.tasks:
                    if not t.completed and not t.parent_id:
                        first_top_level_id = t.id
                        break

            # Determine which tasks should be kept vs removed
            kept_tasks_desc = {}  # For display: reason -> kept task name
            for task in project.tasks:
                if task.completed or task.is_action_group:
                    continue

                mapping_entry = of_id_to_mapping.get(task.id)
                if not mapping_entry or not mapping_entry.get("motion_id"):
                    continue  # Not in Motion, nothing to clean up

                should_remove = False
                kept_by = None

                # Check project-level sequential gate
                if project.sequential:
                    effective_top_level_id = task.parent_id if task.parent_id else task.id
                    if effective_top_level_id != first_top_level_id:
                        should_remove = True
                        # Find what the kept task is
                        if first_top_level_id:
                            first_top = next((t for t in project.tasks if t.id == first_top_level_id), None)
                            if first_top and first_top.is_action_group:
                                # The kept task is the first child of this group
                                group_next = seq_group_next_action.get(first_top_level_id)
                                if group_next:
                                    kept_task = next((t for t in project.tasks if t.id == group_next), None)
                                    kept_by = kept_task.name.strip() if kept_task else first_top.name.strip()
                                else:
                                    kept_by = first_top.name.strip()
                            elif first_top:
                                kept_by = first_top.name.strip()

                # Check action group sequential gate
                if not should_remove and task.parent_id and task.parent_id in seq_group_next_action:
                    if task.id != seq_group_next_action[task.parent_id]:
                        should_remove = True
                        kept_id = seq_group_next_action[task.parent_id]
                        kept_task = next((t for t in project.tasks if t.id == kept_id), None)
                        kept_by = kept_task.name.strip() if kept_task else "unknown"

                if should_remove:
                    tasks_to_remove.append({
                        "of_id": task.id,
                        "motion_id": mapping_entry["motion_id"],
                        "task_name": task.name.strip(),
                        "project_name": project_name,
                        "workspace": mapping_entry.get("workspace", folder_name),
                        "mapping_key": mapping_entry["mapping_key"],
                        "kept_task": kept_by or "first available task",
                    })

    return tasks_to_remove


def delete_motion_task(task_id: str) -> bool:
    """Delete a task from Motion via API."""
    result = MotionSync._make_request("DELETE", f"tasks/{task_id}")
    if result is NOT_FOUND:
        logger.info(f"   Task {task_id} already gone from Motion (404)")
        return True  # Already removed, that's fine
    if result is not None:
        return True
    return False


def remove_from_mapping(mapping_data: dict, mapping_key: str, task_name: str,
                        project_name: str, workspace: str):
    """Remove a task from the mapping file's task_mappings and workspace task lists."""
    # Remove from task_mappings
    if mapping_key in mapping_data.get("task_mappings", {}):
        del mapping_data["task_mappings"][mapping_key]

    # Remove from workspace project tasks
    ws_data = mapping_data.get("workspaces", {}).get(workspace, {})
    proj_data = ws_data.get("projects", {}).get(project_name, {})
    tasks = proj_data.get("tasks", {})
    if task_name in tasks:
        del tasks[task_name]


def main():
    parser = argparse.ArgumentParser(description="Remove non-first-action tasks from Motion for sequential projects")
    parser.add_argument("--execute", action="store_true", help="Actually delete tasks (default is dry run)")
    parser.add_argument("--config", default="config.json", help="Config file path")
    args = parser.parse_args()

    setup_logging()
    dry_run = not args.execute

    if dry_run:
        logger.info("🔍 DRY RUN MODE — no changes will be made\n")
    else:
        logger.info("⚡ EXECUTE MODE — tasks will be deleted from Motion\n")

    # Load config and set API key
    config = Config(args.config)
    api_key = os.getenv("MOTION_API_KEY")
    if not api_key:
        # Load from .env file (same pattern as run_sync.sh)
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "MOTION_API_KEY" in line:
                        key, _, value = line.partition("=")
                        os.environ[key.strip()] = value.strip().strip('"').strip("'")
            api_key = os.getenv("MOTION_API_KEY")
    if not api_key:
        logger.error("❌ MOTION_API_KEY not set. Export it or add to .env")
        sys.exit(1)
    MotionSync.set_api_key(api_key)

    # Load OmniFocus structure via MotionHybridSync
    logger.info("📱 Loading OmniFocus data...")
    hybrid = MotionHybridSync(api_key=api_key, config=config, dry_run=True)
    of_structure = hybrid.load_omnifocus_structure()
    if not of_structure:
        logger.error("❌ Failed to load OmniFocus structure")
        sys.exit(1)

    # Load mapping
    state_file = config.config.get("paths", {}).get("state_file", STATE_FILE)
    logger.info(f"📂 Loading mapping from {state_file}...")
    mapping_data = load_mapping(state_file)

    # Find tasks to remove
    tasks_to_remove = find_tasks_to_remove(of_structure, mapping_data, config)

    if not tasks_to_remove:
        logger.info("\n✅ No tasks to clean up. All sequential projects already have only their next action in Motion.")
        return

    logger.info(f"\n🗑️  Found {len(tasks_to_remove)} task(s) to remove from Motion:\n")
    for i, task in enumerate(tasks_to_remove, 1):
        logger.info(f"  {i}. [{task['workspace']}] {task['project_name']}")
        logger.info(f"     Remove: \"{task['task_name']}\"")
        logger.info(f"     Keep:   \"{task['kept_task']}\" (next action)")
        logger.info(f"     Motion ID: {task['motion_id']}")
        logger.info("")

    if dry_run:
        logger.info("🔍 Dry run complete. Re-run with --execute to delete these tasks.")
        return

    # Execute deletions
    success_count = 0
    fail_count = 0
    for task in tasks_to_remove:
        logger.info(f"🗑️  Deleting: \"{task['task_name']}\" from Motion...")
        if delete_motion_task(task["motion_id"]):
            remove_from_mapping(mapping_data, task["mapping_key"],
                                task["task_name"], task["project_name"], task["workspace"])
            success_count += 1
            logger.info(f"   ✅ Deleted and removed from mapping")
        else:
            fail_count += 1
            logger.error(f"   ❌ Failed to delete from Motion — keeping in mapping")

        time.sleep(0.3)  # Rate limit courtesy

    # Save updated mapping
    if success_count > 0:
        save_mapping(state_file, mapping_data)
        logger.info(f"\n💾 Mapping file updated.")

    logger.info(f"\n📊 Cleanup Results:")
    logger.info(f"   ✅ Deleted: {success_count}")
    if fail_count:
        logger.error(f"   ❌ Failed: {fail_count}")
    logger.info(f"\n🎉 Done. The sync script will now only create next actions for sequential projects.")


if __name__ == "__main__":
    main()
