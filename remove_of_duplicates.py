#!/usr/bin/env python3
"""
OmniFocus Duplicate Remover
Finds and removes duplicate tasks within each project, keeping the oldest (first created).
Uses task name as the unique identifier within each project.

Usage:
  python3 remove_of_duplicates.py          # Dry run (preview only)
  python3 remove_of_duplicates.py --delete  # Actually delete duplicates
"""

import subprocess
import json
import argparse


def load_omnifocus_tasks():
    """Load all tasks from OmniFocus grouped by project."""
    jxa_script = r"""
    (() => {
        const of = Application('OmniFocus');
        if (!of.running()) return JSON.stringify({error: "OmniFocus is not running."});

        const results = [];
        const doc = of.defaultDocument;

        // Process all projects (including those in folders)
        doc.flattenedProjects().forEach(p => {
            if (!p || !p.id || typeof p.id !== 'function') return;
            let isDropped = false;
            try { isDropped = p.dropped(); } catch(e) {}
            if (isDropped) return;

            const projectData = {
                projectId: p.id(),
                projectName: p.name(),
                tasks: []
            };

            try {
                p.flattenedTasks().forEach(t => {
                    if (!t || !t.id || typeof t.id !== 'function') return;
                    let taskDropped = false;
                    try { taskDropped = t.dropped(); } catch(e) {}
                    if (taskDropped) return;

                    let creationDate = null;
                    try { creationDate = t.creationDate() ? t.creationDate().toISOString() : null; } catch(e) {}

                    projectData.tasks.push({
                        id: t.id(),
                        name: t.name(),
                        completed: t.completed(),
                        creationDate: creationDate
                    });
                });
            } catch(e) {}

            if (projectData.tasks.length > 0) {
                results.push(projectData);
            }
        });

        return JSON.stringify(results);
    })();
    """

    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        text=True, capture_output=True, timeout=120, encoding='utf-8'
    )

    data = json.loads(result.stdout)
    if isinstance(data, dict) and 'error' in data:
        print(f"❌ {data['error']}")
        return []
    return data


def find_duplicates(projects):
    """Find duplicate tasks within each project. Keep the oldest."""
    duplicates = []

    for project in projects:
        seen = {}  # task_name → first task
        for task in project['tasks']:
            name = task['name'].strip()
            if name in seen:
                # This is a duplicate - mark the newer one for deletion
                original = seen[name]
                # Keep the one with the earlier creation date
                orig_date = original.get('creationDate') or ''
                dupe_date = task.get('creationDate') or ''

                if dupe_date and orig_date and dupe_date < orig_date:
                    # Current task is older, swap
                    duplicates.append({
                        'project': project['projectName'],
                        'task_name': name,
                        'delete_id': original['id'],
                        'keep_id': task['id'],
                        'delete_date': orig_date,
                        'keep_date': dupe_date
                    })
                    seen[name] = task
                else:
                    duplicates.append({
                        'project': project['projectName'],
                        'task_name': name,
                        'delete_id': task['id'],
                        'keep_id': original['id'],
                        'delete_date': dupe_date,
                        'keep_date': orig_date
                    })
            else:
                seen[name] = task

    return duplicates


def delete_tasks(task_ids):
    """Delete tasks from OmniFocus by ID using AppleScript."""
    deleted = 0
    failed = 0

    for task_id in task_ids:
        applescript = f'''
            tell application "OmniFocus"
                tell default document
                    set matchedTasks to every flattened task whose id is "{task_id}"
                    if (count of matchedTasks) is 0 then return "not_found"
                    delete item 1 of matchedTasks
                    return "success"
                end tell
            end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                text=True, capture_output=True, timeout=10, encoding='utf-8'
            )
            output = result.stdout.strip()
            if output == 'success':
                deleted += 1
            else:
                error = result.stderr.strip() or output
                print(f"   ❌ Failed to delete {task_id[:8]}...: {error}")
                failed += 1
        except Exception as e:
            print(f"   ❌ Error deleting {task_id[:8]}...: {e}")
            failed += 1

    return deleted, failed


def main():
    parser = argparse.ArgumentParser(description="Remove duplicate tasks from OmniFocus")
    parser.add_argument("--delete", action="store_true", help="Actually delete duplicates (default is dry run)")
    args = parser.parse_args()

    print("📱 Loading OmniFocus tasks...")
    projects = load_omnifocus_tasks()
    if not projects:
        print("No projects found.")
        return

    total_tasks = sum(len(p['tasks']) for p in projects)
    print(f"   Found {len(projects)} projects, {total_tasks} tasks")

    print("\n🔍 Finding duplicates...")
    duplicates = find_duplicates(projects)

    if not duplicates:
        print("✅ No duplicates found!")
        return

    print(f"\n⚠️  Found {len(duplicates)} duplicate(s):\n")
    for d in duplicates:
        print(f"   Project: {d['project']}")
        print(f"   Task:    {d['task_name']}")
        print(f"   Keep:    {d['keep_id'][:12]}... (created: {d['keep_date'][:10] if d['keep_date'] else 'unknown'})")
        print(f"   Delete:  {d['delete_id'][:12]}... (created: {d['delete_date'][:10] if d['delete_date'] else 'unknown'})")
        print()

    if args.delete:
        print(f"🗑️  Deleting {len(duplicates)} duplicate(s)...")
        task_ids = [d['delete_id'] for d in duplicates]
        deleted, failed = delete_tasks(task_ids)
        print(f"\n📊 Results: {deleted} deleted, {failed} failed")
    else:
        print("ℹ️  Dry run - no changes made. Run with --delete to remove duplicates.")


if __name__ == "__main__":
    main()
