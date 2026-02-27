#!/usr/bin/env python3
"""
OmniFocus to Motion Sync Script
Version: 3.1 - Stable

This script combines:
1. Motion data mapping creation
2. OmniFocus to Motion synchronization
3. Local JSON-based comparison
4. Automatic data updates

Usage: python3 sync_of_to_motion_hybrid.py [--refresh-mapping] [--sync-only]
"""

import os
import sys
import json
import time
import re
import argparse
import subprocess
import fcntl  # Added for file locking
import traceback  # For error stack traces
import atexit  # For cleanup on exit
import requests  # For HTTP API calls
from typing import Dict, List, Optional, Union, Any
from datetime import datetime, timedelta, timezone

# --- Global Configuration ---
LOCK_FILE = "/tmp/of2motion.lock"
DEFAULT_CONFIG_FILE = "config.json"
METADATA_SEPARATOR = "═" * 40


def build_description(body: str = "", tags: Optional[List[str]] = None,
                      of_url: Optional[str] = None, motion_url: Optional[str] = None) -> str:
    """Build a standardized description/note with metadata below a separator."""
    parts = []
    if body:
        parts.append(body.strip())

    metadata = []
    if tags:
        metadata.append("Tags: " + ", ".join(tags))
    if of_url:
        metadata.append(of_url)
    if motion_url:
        metadata.append(motion_url)

    if metadata:
        parts.append(METADATA_SEPARATOR + "\n")
        parts.append("\n".join(metadata))

    return "\n\n".join(parts)


def extract_body(text: str) -> str:
    """Extract just the body content from a description/note (everything above the separator)."""
    if not text:
        return ""
    if METADATA_SEPARATOR in text:
        return text.split(METADATA_SEPARATOR)[0].strip()
    return text.strip()


def strip_html(text: str) -> str:
    """Strip HTML tags from text, converting <br> and </p> to newlines."""
    if not text or '<' not in text:
        return text or ""
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>\s*<p>', '\n\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()

class Config:
    """Configuration manager for OmniFocus to Motion sync."""
    
    def __init__(self, config_file: str = DEFAULT_CONFIG_FILE):
        self.config_file = config_file
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """Load configuration from JSON file with fallback to defaults."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                print(f"✅ Loaded configuration from {self.config_file}")
                return config_data
            except (json.JSONDecodeError, IOError) as e:
                print(f"⚠️  Error reading config file: {e}. Using defaults.")
        else:
            print(f"⚠️  Config file '{self.config_file}' not found. Using defaults.")
        
        # Default configuration
        return {
            "workspace_mapping": {},
            "workspace_schedules": {
                "My Private Workspace": "Anytime (24/7)"
            },
            "ignored_folders": ["Routines", "Reference"],
            "sync_settings": {
                "api_rate_limit_delay": 0.2,
                "workspace_processing_delay": 0.5
            }
        }
    
    def save_config(self) -> bool:
        """Save current configuration to file."""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            print(f"✅ Configuration saved to {self.config_file}")
            return True
        except IOError as e:
            print(f"❌ Error saving config: {e}")
            return False
    
    @property
    def workspace_mapping(self) -> Dict[str, str]:
        """Get OmniFocus folder to Motion workspace mapping."""
        return self.config.get("workspace_mapping", {})
    
    @property
    def workspace_schedules(self) -> Dict[str, str]:
        """Get Motion workspace to schedule mapping."""
        return self.config.get("workspace_schedules", {})
    
    @property
    def ignored_folders(self) -> List[str]:
        """Get list of OmniFocus folders to ignore."""
        return self.config.get("ignored_folders", [])
    
    @property
    def api_rate_limit_delay(self) -> float:
        """Delay between API requests (seconds)."""
        return self.config.get("sync_settings", {}).get("api_rate_limit_delay", 0.2)
    
    @property
    def workspace_processing_delay(self) -> float:
        """Delay between processing workspaces (seconds)."""
        return self.config.get("sync_settings", {}).get("workspace_processing_delay", 0.5)

def acquire_lock():
    """Ensures only one instance of the script runs at a time."""
    # We use a persistent handle so the lock stays active for the script's life
    fp = open(LOCK_FILE, 'w')
    try:
        # LOCK_EX: Exclusive lock
        # LOCK_NB: Non-blocking (fail immediately if locked)
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Ensure lock is released on exit
        atexit.register(lambda: (fp.close(), os.remove(LOCK_FILE) if os.path.exists(LOCK_FILE) else None))
        
        return fp
    except IOError:
        fp.close()  # Close file handle if lock fails
        print("⛔ Another instance is already running. Exiting to prevent API conflicts.")
        sys.exit(0)

# --- Data Classes ---
class OFTask:
    """OmniFocus task data structure."""
    def __init__(self, id, name, note="", due_date=None, defer_date=None, duration_minutes=None, 
                 completed=False, url=None, flagged=False, of_modification_date=None, 
                 contexts=None, repeat_rule=None, of_priority=None):
        self.id = id
        self.name = name
        self.note = note
        self.due_date = due_date
        self.defer_date = defer_date  # ✅ NEW: Start date
        self.duration_minutes = duration_minutes
        self.completed = completed
        self.url = url
        self.flagged = flagged
        self.of_modification_date = of_modification_date
        self.contexts = contexts or []  # ✅ NEW: Tags/contexts
        self.repeat_rule = repeat_rule  # ✅ NEW: Recurring task rule
        self.of_priority = of_priority  # ✅ NEW: Explicit priority (high/medium/low)

class OFProject:
    """OmniFocus project data structure."""
    def __init__(self, id, name, url=None, of_modification_date=None, completed=False, tasks=None):
        self.id = id
        self.name = name
        self.url = url
        self.of_modification_date = of_modification_date
        self.completed = completed
        self.tasks = tasks or []

class OFFolder:
    """OmniFocus folder data structure."""
    def __init__(self, id, name, projects=None):
        self.id = id
        self.name = name
        self.projects = projects or []

# --- Motion API Interaction Class ---
class MotionSync:
    """Handles all Motion API operations with proper error handling and rate limiting."""

    _api_key: Optional[str] = None
    MAX_RETRIES = 3

    @classmethod
    def set_api_key(cls, api_key: str):
        """Set the API key for all Motion API requests."""
        cls._api_key = api_key

    @classmethod
    def _make_request(cls, method: str, endpoint: str, json_data: Optional[Dict] = None,
                      params: Optional[Dict] = None, _retry_count: int = 0) -> Optional[Dict]:
        """Make HTTP request to Motion API with proper error handling."""
        url = f"https://api.usemotion.com/v1/{endpoint}"
        headers = {
            "X-API-Key": cls._api_key or os.getenv("MOTION_API_KEY"),
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.request(method, url, headers=headers, params=params, json=json_data)
            
            if response.status_code in [200, 201]:
                return response.json() if response.content else {"status": "success"}
            elif response.status_code == 204:
                return {"status": "success"}
            elif response.status_code == 429:
                if _retry_count >= cls.MAX_RETRIES:
                    print(f"❌ Rate limited {cls.MAX_RETRIES} times on {method} {endpoint}. Giving up.")
                    return None
                try:
                    retry_after = int(response.headers.get("Retry-After", 60))
                except (ValueError, TypeError):
                    retry_after = 60
                print(f"⏳ Rate limited. Waiting {retry_after} seconds... (attempt {_retry_count + 1}/{cls.MAX_RETRIES})")
                time.sleep(retry_after)
                return cls._make_request(method, endpoint, json_data, params, _retry_count=_retry_count + 1)
            else:
                print(f"❌ HTTP {response.status_code} on {method} {endpoint}: {response.text}")
                return None
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Request error on {method} {endpoint}: {e}")
            return None

    @classmethod
    def create_project_in_workspace(cls, workspace_id: str, project_name: str, of_project_url: Optional[str] = None) -> Optional[Dict]:
        print(f" M-> Creating project: {project_name} in workspace {workspace_id}...")
        payload = {"name": project_name, "workspaceId": workspace_id}
        if of_project_url:
            payload["description"] = f"OmniFocus Project Link: {of_project_url}"
        return cls._make_request("POST", "projects", json_data=payload)

    @classmethod
    def create_task_in_project(cls, project_id: str, workspace_id: str, task_name: str,
                               description: Optional[str] = None, of_task_url: Optional[str] = None,
                               due_date_str: Optional[str] = None, defer_date_str: Optional[str] = None,
                               task_duration: Optional[Union[int, str]] = None,
                               schedule_name_to_use: Optional[str] = None,
                               priority: str = "MEDIUM",
                               labels: Optional[List[str]] = None) -> Optional[Dict]:
        payload = {
            "name": task_name, "projectId": project_id,
            "workspaceId": workspace_id, "priority": priority
        }
        
        desc = build_description(
            body=description or "",
            tags=labels if labels and isinstance(labels, list) else None,
            of_url=of_task_url
        )
        if desc:
            payload["description"] = desc

        if due_date_str: payload["dueDate"] = due_date_str

        # Note: scheduledStart is not supported by Motion API; defer dates are
        # stored in the task description instead
        if defer_date_str:
            start_note = f"Start date: {defer_date_str}"
            if "description" in payload:
                payload["description"] = start_note + "\n\n" + payload["description"]
            else:
                payload["description"] = start_note

        if schedule_name_to_use:
            payload["autoScheduled"] = {"schedule": schedule_name_to_use}
            if not due_date_str:
                payload["dueDate"] = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")

        if task_duration is not None: payload["duration"] = task_duration
        
        return cls._make_request("POST", "tasks", json_data=payload)

    @classmethod
    def update_task(cls, task_id: str, updates: Dict[str, Any]) -> Optional[Dict]:
        """Update an existing task in Motion and return the full response object on success."""
        payload = {}
        if "priority" in updates: payload["priority"] = updates["priority"]
        if "duration" in updates: payload["duration"] = updates.get("duration")
        if "dueDate" in updates: payload["dueDate"] = updates.get("dueDate")
        if "description" in updates: payload["description"] = updates.get("description")

        if not payload:
            print(f" M-> Info: No valid fields to update for task {task_id}. Skipping.")
            return {"id": task_id, **updates}

        print(f"    M-> Sending update for task {task_id} with payload: {payload}")
        response = cls._make_request("PATCH", f"tasks/{task_id}", json_data=payload)
        
        if response is not None:
            print(f" M-> ✨ Successfully updated task (ID: {task_id})")
            return response
        else:
            print(f"🚨 Error updating task {task_id}. API response was None.")
            return None

    @classmethod
    def complete_task(cls, task_id: str) -> bool:
        """Mark a task as completed in Motion. Returns True on success."""
        print(f" M-> Completing task (ID: {task_id})")
        response = cls._make_request('PATCH', f'tasks/{task_id}', json_data={'status': 'completed'})
        if response is not None:
            print(f"     M-> ✨ Completed task successfully.")
            return True
        print(f"     M-> ❌ Failed to complete task.")
        return False

# --- OmniFocus Manager (Write Operations) ---
class OmniFocusManager:
    """Handles writing data back to OmniFocus via JXA."""
    
    @staticmethod
    def complete_task(task_id: str) -> bool:
        """Mark an OmniFocus task as complete using AppleScript.

        Uses try/on error to handle inbox tasks that reject mark complete,
        falling back to assigning to first available project then completing.
        """
        applescript = f'''
            tell application "OmniFocus"
                if not running then return "omnifocus_not_running"
                tell default document
                    set matchedTasks to every flattened task whose id is "{task_id}"
                    if (count of matchedTasks) is 0 then return "task_not_found"
                    set theTask to item 1 of matchedTasks
                    try
                        mark complete theTask
                        return "success"
                    on error errMsg
                        -- Inbox tasks can't be completed directly; assign to a project first
                        if errMsg contains "inbox" then
                            try
                                set assigned container of theTask to first flattened project
                                mark complete theTask
                                return "success"
                            on error errMsg2
                                return "error:" & errMsg2
                            end try
                        else
                            return "error:" & errMsg
                        end if
                    end try
                end tell
            end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                text=True, capture_output=True, timeout=15, encoding='utf-8'
            )

            output = result.stdout.strip()

            if output == 'success':
                return True
            elif output == 'task_not_found':
                print(f"      ⚠️  OmniFocus task {task_id[:8]}... not found")
                return False
            elif output == 'omnifocus_not_running':
                print(f"      ❌ OmniFocus is not running")
                return False
            elif output.startswith('error:'):
                print(f"      ❌ OmniFocus error: {output[6:]}")
                return False
            else:
                error_msg = result.stderr.strip() or output
                print(f"      ❌ OmniFocus error: {error_msg}")
                return False

        except subprocess.TimeoutExpired:
            print(f"      ❌ OmniFocus script timeout")
            return False
        except Exception as e:
            print(f"      ❌ Failed to complete OmniFocus task: {e}")
            return False
    
    @staticmethod
    def update_task(task_id: str, updates: Dict[str, Any]) -> bool:
        """Update OmniFocus task fields using AppleScript."""
        update_lines = []

        if 'flagged' in updates:
            val = "true" if updates['flagged'] else "false"
            update_lines.append(f'set flagged of theTask to {val}')

        if 'due_date' in updates and updates['due_date']:
            update_lines.append(f'set due date of theTask to date "{updates["due_date"]}"')

        if 'defer_date' in updates and updates['defer_date']:
            update_lines.append(f'set defer date of theTask to date "{updates["defer_date"]}"')

        if 'duration_minutes' in updates and updates['duration_minutes']:
            update_lines.append(f'set estimated minutes of theTask to {updates["duration_minutes"]}')

        if 'note' in updates and updates['note'] is not None:
            escaped_note = updates['note'].replace('\\', '\\\\').replace('"', '\\"')
            update_lines.append(f'set note of theTask to "{escaped_note}"')

        if not update_lines:
            return True  # Nothing to update

        update_block = "\n                    ".join(update_lines)

        applescript = f'''
            tell application "OmniFocus"
                if not running then return "omnifocus_not_running"
                tell default document
                    set matchedTasks to every flattened task whose id is "{task_id}"
                    if (count of matchedTasks) is 0 then return "task_not_found"
                    set theTask to item 1 of matchedTasks
                    {update_block}
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
                return True
            else:
                error_msg = result.stderr.strip() or output
                print(f"      ❌ OmniFocus update error: {error_msg}")
                return False

        except Exception as e:
            print(f"      ❌ Failed to update OmniFocus task: {e}")
            return False

    @staticmethod
    def create_task(project_name: str, task_name: str, note: str = "",
                    due_date: Optional[str] = None, flagged: bool = False,
                    estimated_minutes: Optional[int] = None) -> Optional[str]:
        """Create a new task in an OmniFocus project. Returns the new task ID or None."""
        escaped_name = task_name.replace('\\', '\\\\').replace('"', '\\"')
        escaped_note = note.replace('\\', '\\\\').replace('"', '\\"') if note else ''
        escaped_project = project_name.replace('\\', '\\\\').replace('"', '\\"')

        # Build task properties
        props = [f'name:"{escaped_name}"']
        if escaped_note:
            props.append(f'note:"{escaped_note}"')
        if flagged:
            props.append('flagged:true')
        if estimated_minutes:
            props.append(f'estimated minutes:{estimated_minutes}')

        props_str = ", ".join(props)

        # Due date needs to be set separately since AppleScript date parsing is tricky
        due_date_line = ""
        if due_date:
            # due_date expected as "YYYY-MM-DD"
            due_date_line = f'''
                    set due date of newTask to date "{due_date}"'''

        applescript = f'''
            tell application "OmniFocus"
                if not running then return "omnifocus_not_running"
                tell default document
                    set matchedProjects to every flattened project whose name is "{escaped_project}"
                    if (count of matchedProjects) is 0 then return "project_not_found"
                    set theProject to item 1 of matchedProjects
                    set newTask to make new task at end of tasks of theProject with properties {{{props_str}}}
                    {due_date_line}
                    return id of newTask
                end tell
            end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                text=True, capture_output=True, timeout=10, encoding='utf-8'
            )

            output = result.stdout.strip()

            if output == 'omnifocus_not_running':
                print(f"      ❌ OmniFocus is not running")
                return None
            elif output == 'project_not_found':
                print(f"      ⚠️  OmniFocus project '{project_name}' not found")
                return None
            elif output and not output.startswith('error'):
                return output  # This is the new task ID
            else:
                error_msg = result.stderr.strip() or output
                print(f"      ❌ OmniFocus create error: {error_msg}")
                return None

        except subprocess.TimeoutExpired:
            print(f"      ❌ OmniFocus script timeout")
            return None
        except Exception as e:
            print(f"      ❌ Failed to create OmniFocus task: {e}")
            return None

# --- Task ID Mapping Manager ---
class TaskIDMapper:
    """Manages bidirectional mapping between OmniFocus and Motion task IDs."""
    
    def __init__(self, state_data: Dict):
        self.state_data = state_data
        if 'task_mappings' not in self.state_data:
            self.state_data['task_mappings'] = {}
    
    def add_mapping(self, of_id: str, motion_id: str, workspace: str, project: str, task_name: str):
        """Store bidirectional ID mapping."""
        mapping_key = self._create_key(of_id, motion_id)
        self.state_data['task_mappings'][mapping_key] = {
            'of_id': of_id,
            'motion_id': motion_id,
            'workspace': workspace,
            'project': project,
            'task_name': task_name,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
    
    def get_motion_id_from_of(self, of_id: str) -> Optional[str]:
        """Find Motion ID given OmniFocus ID."""
        for mapping in self.state_data['task_mappings'].values():
            if mapping.get('of_id') == of_id:
                return mapping.get('motion_id')
        return None
    
    def get_of_id_from_motion(self, motion_id: str) -> Optional[str]:
        """Find OmniFocus ID given Motion ID."""
        for mapping in self.state_data['task_mappings'].values():
            if mapping.get('motion_id') == motion_id:
                return mapping.get('of_id')
        return None
    
    def get_mapping(self, of_id: Optional[str] = None, motion_id: Optional[str] = None) -> Optional[Dict]:
        """Get full mapping by either ID."""
        for mapping in self.state_data['task_mappings'].values():
            if (of_id and mapping.get('of_id') == of_id) or \
               (motion_id and mapping.get('motion_id') == motion_id):
                return mapping
        return None
    
    def update_task_name(self, of_id: str, motion_id: str, new_name: str):
        """Update task name in mapping (for rename detection)."""
        mapping_key = self._create_key(of_id, motion_id)
        if mapping_key in self.state_data['task_mappings']:
            self.state_data['task_mappings'][mapping_key]['task_name'] = new_name
            self.state_data['task_mappings'][mapping_key]['updated_at'] = datetime.now(timezone.utc).isoformat()
    
    def remove_mapping(self, of_id: Optional[str] = None, motion_id: Optional[str] = None):
        """Remove mapping when task is deleted."""
        keys_to_remove = []
        for key, mapping in self.state_data['task_mappings'].items():
            if (of_id and mapping.get('of_id') == of_id) or \
               (motion_id and mapping.get('motion_id') == motion_id):
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self.state_data['task_mappings'][key]
    
    @staticmethod
    def _create_key(of_id: str, motion_id: str) -> str:
        """Create composite key for mapping."""
        return f"{of_id}::{motion_id}"

# --- Main Sync Class ---
class MotionHybridSync:
    def __init__(self, api_key: str, config: Optional[Config] = None):
        self.api_key = api_key
        
        # Load configuration (or use provided config object)
        self.config = config if config else Config()
        
        # Load mappings from config
        self.workspace_schedule_mapping = self.config.workspace_schedules
        self.workspace_mapping = self.config.workspace_mapping
        self.ignored_folders = self.config.ignored_folders
        
        self.last_sync_timestamp = None
        self.id_mapper = None  # ✅ Will be initialized when state is loaded

    @staticmethod
    def map_of_priority_to_motion(of_task: OFTask) -> str:
        """
        Map OmniFocus priority to Motion priority.
        Priority hierarchy:
        1. Flagged = ASAP (urgent!)
        2. High priority tag = HIGH
        3. Medium priority tag = MEDIUM (default)
        4. Low priority tag = LOW
        """
        # Flagged tasks are always ASAP (most urgent)
        if getattr(of_task, 'flagged', False):
            return "ASAP"
        
        # Check for explicit priority tags
        of_priority = getattr(of_task, 'of_priority', None)
        if of_priority:
            priority_map = {
                'high': 'HIGH',
                'medium': 'MEDIUM',
                'low': 'LOW'
            }
            return priority_map.get(of_priority.lower(), 'MEDIUM')
        
        # Default to MEDIUM if no priority specified
        return "MEDIUM"

    def check_local_data_exists(self) -> bool:
        """Check if local mapping file exists."""
        return os.path.exists('motion_hierarchical_mapping.json')
    
    def get_workspaces(self) -> Dict[str, Dict]:
        print("🔍 Fetching Motion workspaces...")
        # ✅ FIXED: Use MotionSync._make_request() instead of duplicate method
        data = MotionSync._make_request("GET", "workspaces")
        if not data or "workspaces" not in data:
            print("❌ Failed to fetch workspaces")
            return {}
        workspaces = {w.get("name"): w for w in data["workspaces"] if w.get("name")}
        print(f"✅ Found {len(workspaces)} workspaces")
        return workspaces
    
    def get_projects_for_workspace(self, workspace_id: str, workspace_name: str) -> Dict[str, Dict]:
        print(f"  📁 Fetching projects for workspace: {workspace_name}")
        projects, cursor = {}, None
        while True:
            params = {"workspaceId": workspace_id}
            if cursor: params["cursor"] = cursor
            data = MotionSync._make_request("GET", "projects", params=params)
            if not data or "projects" not in data: break
            for p in data["projects"]:
                if p_name := p.get("name"): projects[p_name] = p
            if not (cursor := data.get("meta", {}).get("nextCursor")): break
        print(f"  📊 Total projects in {workspace_name}: {len(projects)}")
        return projects
    
    def get_tasks_for_project(self, project_id: str, project_name: str) -> Dict[str, Dict]:
        print(f"      Fetching tasks for project: {project_name}")
        tasks, cursor = {}, None
        while True:
            params = {"projectId": project_id}
            if cursor: params["cursor"] = cursor
            data = MotionSync._make_request("GET", "tasks", params=params)
            if not data or "tasks" not in data: break
            for t in data["tasks"]:
                if t_name := t.get("name"): tasks[t_name] = t
            if not (cursor := data.get("meta", {}).get("nextCursor")): break
        print(f"      📊 Total tasks in project '{project_name}': {len(tasks)}")
        return tasks
    
    def create_comprehensive_mapping(self) -> Dict:
        print("🚀 Starting comprehensive Motion mapping creation...")
        workspaces = self.get_workspaces()
        if not workspaces: return {}

        # Preserve existing task_mappings when refreshing
        existing_data = self.load_motion_data_from_file()
        existing_mappings = existing_data.get('task_mappings', {})

        mapping_data = {"workspaces": {}, "task_mappings": existing_mappings}
        total_projects, total_tasks = 0, 0

        for ws_name, ws_data in workspaces.items():
            print(f"\n🏢 Processing workspace: {ws_name}")
            ws_id = ws_data["id"]
            projects = self.get_projects_for_workspace(ws_id, ws_name)
            for p_name, p_data in projects.items():
                p_id = p_data["id"]
                p_data["tasks"] = self.get_tasks_for_project(p_id, p_name)
                total_tasks += len(p_data["tasks"])
                time.sleep(self.config.api_rate_limit_delay)
            mapping_data["workspaces"][ws_name] = {
                "id": ws_id, "type": ws_data.get("type"),
                "labels": ws_data.get("labels", []), "projects": projects
            }
            total_projects += len(projects)
            time.sleep(self.config.workspace_processing_delay)

        print(f"\n🎯 HIERARCHICAL MAPPING COMPLETE! Workspaces: {len(workspaces)}, Projects: {total_projects}, Tasks: {total_tasks}")
        self.save_motion_data_to_file(mapping_data, update_timestamp=False)
        return mapping_data

    def load_motion_data_from_file(self) -> Dict:
        """Loads the entire motion data structure from the local JSON file."""
        filename = "motion_hierarchical_mapping.json"
        if not os.path.exists(filename):
            print(f"⚠️ No local data file found at '{filename}'. A new one will be created.")
            data = {"metadata": {}, "workspaces": {}, "task_mappings": {}}
            self.id_mapper = TaskIDMapper(data)  # ✅ Initialize mapper
            return data
        
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # ✅ Ensure task_mappings exists
            if 'task_mappings' not in data:
                data['task_mappings'] = {}
            
            # ✅ Initialize ID mapper with loaded data
            self.id_mapper = TaskIDMapper(data)
            
            return data
        except (json.JSONDecodeError, IOError) as e:
            print(f"❌ Error reading '{filename}': {e}. Starting with empty data.")
            data = {"metadata": {}, "workspaces": {}, "task_mappings": {}}
            self.id_mapper = TaskIDMapper(data)
            return data

    def save_motion_data_to_file(self, motion_data: Dict, update_timestamp: bool = True):
        """
        Updates the JSON file with new data and optionally updates the sync timestamp.
        """
        filename = "motion_hierarchical_mapping.json"
        
        current_data = motion_data
        if 'metadata' not in current_data: current_data['metadata'] = {}

        current_data['metadata']['total_workspaces'] = len(current_data.get('workspaces', {}))
        current_data['metadata']['total_projects'] = sum(len(ws.get("projects", {})) for ws in current_data.get('workspaces', {}).values())
        current_data['metadata']['total_tasks'] = sum(sum(len(p.get("tasks", {})) for p in ws.get("projects", {}).values()) for ws in current_data.get('workspaces', {}).values())
        
        if update_timestamp:
            new_timestamp = datetime.now(timezone.utc).isoformat()
            current_data['metadata']['last_sync_timestamp'] = new_timestamp
            print(f"💾 Saving with new sync timestamp: {new_timestamp}")
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(current_data, f, indent=2, ensure_ascii=False)
            print(f"✅ Motion data saved successfully to {filename}")
        except IOError as e:
            print(f"❌ Error saving motion data to file: {e}")

    def load_omnifocus_structure(self):
        """Load OmniFocus data structure using JXA script."""
        print("📱 Loading OmniFocus structure...")
        jxa_script = r"""
        (() => {
            const of = Application('OmniFocus');
            if (!of.running()) { return JSON.stringify({error: "OmniFocus is not running."}); }
            const formatDate = (date) => {
                if (!date || !(date instanceof Date) || isNaN(date.getTime())) return null;
                return `${date.getFullYear()}-${(date.getMonth() + 1).toString().padStart(2, '0')}-${date.getDate().toString().padStart(2, '0')}`;
            };
            const processProject = (p) => {
                if (!p || !p.id || typeof p.id !== 'function' || !p.name || typeof p.name !== 'function') return null;
                let projectStatus = ''; try { projectStatus = p.status(); } catch(e) {}
                let isDropped = false; try { isDropped = p.dropped(); } catch(e) {}
                if (isDropped || projectStatus === 'on hold') return null;
                const projectData = {
                    id: p.id(), name: p.name(), url: 'omnifocus:///project/' + p.id(),
                    modificationDate: p.modificationDate() ? p.modificationDate().toISOString() : null,
                    completed: p.completed(), tasks: []
                };
                try {
                    p.flattenedTasks().forEach(t => {
                        if (!t || !t.id || typeof t.id !== 'function' || !t.name || typeof t.name !== 'function') return;
                        const taskName = t.name();
                        if (!taskName || taskName === '-----------') return;
                        
                        let isDropped = false; try { isDropped = t.dropped(); } catch(e) {}
                        if (isDropped) return;

                        let estimatedMins = null;
                        try { const mr = t.estimatedMinutes(); if (typeof mr === 'number' && mr > 0) estimatedMins = mr; } catch (e) {}
                        
                        // ✅ NEW: Capture defer date (start date)
                        let deferDate = null;
                        try { deferDate = formatDate(t.deferDate()); } catch (e) {}
                        
                        // ✅ NEW: Capture contexts/tags
                        let contexts = [];
                        let ofPriority = null;  // Track OF priority separately
                        try { 
                            const taskTags = t.tags();
                            taskTags.forEach(tag => {
                                const tagName = tag.name();
                                contexts.push(tagName);
                                
                                // Detect priority tags
                                const tagLower = tagName.toLowerCase();
                                if (tagLower === 'high' || tagLower === 'high priority') ofPriority = 'high';
                                else if (tagLower === 'medium' || tagLower === 'medium priority') ofPriority = 'medium';
                                else if (tagLower === 'low' || tagLower === 'low priority') ofPriority = 'low';
                            });
                        } catch (e) {}
                        
                        // ✅ NEW: Capture repeat rule
                        let repeatRule = null;
                        try {
                            const rule = t.repetitionRule();
                            if (rule) repeatRule = rule.ruleString();
                        } catch (e) {}
                        
                        projectData.tasks.push({
                            id: t.id(), name: taskName, note: t.note() || null, completed: t.completed(),
                            url: 'omnifocus:///task/' + t.id(), dueDate: formatDate(t.dueDate()),
                            deferDate: deferDate,  // ✅ NEW
                            durationMinutes: estimatedMins, flagged: t.flagged(),
                            contexts: contexts,  // ✅ NEW
                            ofPriority: ofPriority,  // ✅ NEW: Explicit priority
                            repeatRule: repeatRule,  // ✅ NEW
                            modificationDate: t.modificationDate() ? t.modificationDate().toISOString() : null
                        });
                    });
                } catch (e) {}
                return projectData;
            };
            const structure = [];
            of.defaultDocument.folders().forEach(f => {
                if (!f || !f.name || typeof f.name !== 'function') return;
                const folderData = { id: f.id(), name: f.name(), projects: [] };
                f.flattenedProjects().forEach(p => {
                    const projectData = processProject(p);
                    if (projectData) folderData.projects.push(projectData);
                });
                structure.push(folderData);
            });
            const standaloneFolderData = { id: 'standalone', name: 'Standalone Projects', projects: [] };
            of.defaultDocument.projects().forEach(p => {
                const projectData = processProject(p);
                if (projectData) standaloneFolderData.projects.push(projectData);
            });
            if (standaloneFolderData.projects.length > 0) structure.push(standaloneFolderData);
            return JSON.stringify(structure);
        })();
        """
        try:
            result = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa_script], text=True, capture_output=True, check=True, encoding='utf-8')
            raw_data = json.loads(result.stdout)
            
            # ✅ FIXED: Check for error response from OmniFocus
            if isinstance(raw_data, dict) and 'error' in raw_data:
                print(f"❌ OmniFocus error: {raw_data['error']}")
                return []
            
            of_structure = []
            for fr_data in raw_data:
                folder = OFFolder(id=fr_data['id'], name=fr_data['name'])
                for pr_data in fr_data.get('projects', []):
                    project = OFProject(id=pr_data['id'], name=pr_data['name'], url=pr_data.get('url'),
                                        of_modification_date=pr_data.get('modificationDate'), completed=pr_data.get('completed', False))
                    for tr_data in pr_data.get('tasks', []):
                        task = OFTask(id=tr_data['id'], name=tr_data['name'], note=tr_data.get('note'),
                                      completed=tr_data.get('completed', False), url=tr_data.get('url'),
                                      due_date=tr_data.get('dueDate'),
                                      defer_date=tr_data.get('deferDate'),  # ✅ NEW
                                      duration_minutes=tr_data.get('durationMinutes'),
                                      flagged=tr_data.get('flagged', False),
                                      of_modification_date=tr_data.get('modificationDate'),
                                      contexts=tr_data.get('contexts', []),  # ✅ NEW
                                      repeat_rule=tr_data.get('repeatRule'),  # ✅ NEW
                                      of_priority=tr_data.get('ofPriority'))  # ✅ NEW
                        project.tasks.append(task)
                    folder.projects.append(project)
                of_structure.append(folder)
            
            print(f"📱 Found {len(of_structure)} OF folders, {sum(len(f.projects) for f in of_structure)} projects, {sum(len(p.tasks) for f in of_structure for p in f.projects)} tasks.")
            return of_structure
        except Exception as e: 
            print(f"🚨 Unexpected OF error: {e}")
            return []

    def sync_omnifocus_to_motion(self):
        """Real OmniFocus to Motion synchronization using local JSON data."""
        print(" Starting OmniFocus to Motion synchronization...")
        local_data = self.load_motion_data_from_file()
        # Load OF structure only if not already loaded (standalone mode)
        if not self.of_structure:
            self.of_structure = self.load_omnifocus_structure()
        if not self.of_structure:
            print("❌ Failed to load OmniFocus structure. Cannot proceed with sync.")
            return
        self.perform_sync_comparison_from_structure(local_data)
        print("✅ Synchronization completed!")

    def refresh_motion_task_statuses(self, motion_data: Dict):
        """Check mapped tasks for completions in Motion.

        Optimization: Uses the API-sourced active task IDs from
        refresh_motion_tasks_from_api (which only returns non-completed tasks).
        If a mapped task is NOT in that set, it may have been completed -
        check it individually via GET /tasks/{id}.
        """
        if not self.id_mapper:
            return motion_data

        mappings = self.id_mapper.state_data.get('task_mappings', {})
        if not mappings:
            return motion_data

        # Use API-sourced active IDs (set by refresh_motion_tasks_from_api)
        # This is what Motion actually says is active, not stale local data
        active_motion_ids = getattr(self, '_api_active_motion_ids', set())

        # Only check tasks that are mapped but NOT in the API active list
        # (they may have been completed or deleted in Motion)
        tasks_to_check = []
        for mapping in mappings.values():
            motion_id = mapping.get('motion_id')
            if not motion_id:
                continue
            # Skip if the API confirmed this task is still active
            if motion_id in active_motion_ids:
                continue
            # Skip if already marked completed in local data
            ws_name = mapping.get('workspace', '')
            proj_name = mapping.get('project', '')
            task_name = mapping.get('task_name', '')
            proj_tasks = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('tasks', {})
            if task_name in proj_tasks:
                local_status = (proj_tasks[task_name].get('status') or {}).get('name', '').lower()
                if local_status == 'completed':
                    continue
            tasks_to_check.append(mapping)

        if not tasks_to_check:
            print("🔄 No tasks need completion status check")
            return motion_data

        print(f"🔄 Checking {len(tasks_to_check)} task(s) for Motion completion status...")
        completed_count = 0

        for mapping in tasks_to_check:
            motion_id = mapping.get('motion_id')

            # Fetch individual task status from Motion API
            task_data = MotionSync._make_request("GET", f"tasks/{motion_id}")
            if not task_data:
                continue

            is_completed = task_data.get('completed', False)
            if not is_completed:
                time.sleep(self.config.api_rate_limit_delay)
                continue

            # Update local data to reflect completion
            ws_name = mapping.get('workspace', '')
            proj_name = mapping.get('project', '')
            task_name = mapping.get('task_name', '')

            proj_tasks = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('tasks', {})
            if task_name in proj_tasks:
                proj_tasks[task_name]['status'] = {'name': 'Completed', 'isResolvedStatus': True}
                proj_tasks[task_name]['completed'] = True
                proj_tasks[task_name]['completedTime'] = task_data.get('completedTime')
                completed_count += 1
                print(f"   ✅ Detected completion: '{task_name}'")

            time.sleep(self.config.api_rate_limit_delay)

        print(f"   📊 Found {completed_count} newly completed task(s) in Motion")
        if completed_count > 0:
            self.save_motion_data_to_file(motion_data, update_timestamp=False)
        return motion_data

    def refresh_motion_tasks_from_api(self, motion_data: Dict) -> Dict:
        """Fetch current tasks from Motion API and merge into local data.

        This does two things:
        1. Discovers tasks created directly in Motion (not in local JSON)
        2. Builds a set of active Motion task IDs for completion detection

        The active IDs set is stored on self so refresh_motion_task_statuses
        can use it to identify tasks that disappeared (completed/deleted).
        """
        print("🔄 Refreshing Motion tasks from API...")
        new_task_count = 0
        self._api_active_motion_ids = set()  # Track what Motion API says is active

        for ws_name, ws_data in motion_data.get('workspaces', {}).items():
            ws_id = ws_data.get('id')
            if not ws_id:
                continue

            for proj_name, proj_data in ws_data.get('projects', {}).items():
                proj_id = proj_data.get('id')
                if not proj_id:
                    continue

                # Fetch current tasks from Motion API for this project
                api_tasks = self.get_tasks_for_project(proj_id, proj_name)
                local_tasks = proj_data.get('tasks', {})

                # Track all active task IDs from the API
                for task_data in api_tasks.values():
                    if task_data.get('id'):
                        self._api_active_motion_ids.add(task_data['id'])

                # Merge any new tasks that aren't in local data
                for task_name, task_data in api_tasks.items():
                    if task_name not in local_tasks:
                        local_tasks[task_name] = task_data
                        new_task_count += 1

                proj_data['tasks'] = local_tasks
                time.sleep(self.config.api_rate_limit_delay)

        if new_task_count > 0:
            print(f"   📊 Discovered {new_task_count} new task(s) from Motion API")
            self.save_motion_data_to_file(motion_data, update_timestamp=False)
        else:
            print("   📊 No new tasks found in Motion")

        return motion_data

    def sync_motion_to_omnifocus(self):
        """Sync Motion changes back to OmniFocus (reverse sync)."""
        print("\n🔄 Starting Motion → OmniFocus synchronization...")

        # Refresh from Motion API to pick up changes and new tasks
        motion_data = self.load_motion_data_from_file()
        motion_data = self.refresh_motion_tasks_from_api(motion_data)
        motion_data = self.refresh_motion_task_statuses(motion_data)

        # Reuse OF structure loaded at start of bidirectional sync
        if not self.of_structure:
            self.of_structure = self.load_omnifocus_structure()
        if not self.of_structure:
            print("❌ Failed to load OmniFocus structure. Cannot proceed with reverse sync.")
            return

        # Create reverse sync plan (may add auto-mappings to id_mapper)
        reverse_sync_plan = self.create_reverse_sync_plan(motion_data, self.of_structure)

        # Execute reverse sync, passing motion_data to avoid re-loading (which discards auto-mappings)
        self.execute_reverse_sync_plan(reverse_sync_plan, motion_data)

        print("✅ Reverse synchronization completed!")
    
    def create_reverse_sync_plan(self, motion_data: Dict, of_structure: List[OFFolder]) -> Dict:
        """
        Create a plan for syncing Motion → OmniFocus.
        Handles completions and new task creation.
        """
        print("📋 Creating reverse sync plan (Motion → OmniFocus)...")

        # Build reverse workspace mapping (Motion workspace → OF folder)
        reverse_ws_mapping = {v: k for k, v in self.workspace_mapping.items()}

        reverse_sync_plan = {
            'of_tasks_to_complete': [],
            'of_tasks_to_update': [],
            'of_tasks_to_create': []
        }

        # Create lookup maps of OmniFocus tasks
        of_tasks_by_id = {}
        of_tasks_by_project_and_name = {}  # (project_name, task_name) → OFTask
        of_projects_by_folder = {}
        for folder in of_structure:
            of_projects_by_folder[folder.name] = {p.name for p in folder.projects}
            for project in folder.projects:
                for task in project.tasks:
                    of_tasks_by_id[task.id] = task
                    of_tasks_by_project_and_name[(project.name, task.name.strip())] = task

        # Iterate through Motion workspaces
        for ws_name, ws_data in motion_data.get('workspaces', {}).items():
            for proj_name, proj_data in ws_data.get('projects', {}).items():
                for task_name, motion_task in proj_data.get('tasks', {}).items():

                    motion_id = motion_task.get('id')
                    if not motion_id:
                        continue

                    # Skip completed Motion tasks for creation (only sync completions)
                    motion_status = (motion_task.get('status') or {}).get('name', '').lower()
                    motion_completed = motion_status == 'completed'

                    # Use ID mapper to find corresponding OmniFocus task
                    of_id = self.id_mapper.get_of_id_from_motion(motion_id) if self.id_mapper else None

                    if not of_id:
                        # No mapping exists - check if task already exists in OF by name
                        existing_of_task = of_tasks_by_project_and_name.get((proj_name, task_name))
                        if existing_of_task:
                            # Task already exists in OF - create the mapping, don't duplicate
                            if self.id_mapper:
                                self.id_mapper.add_mapping(
                                    of_id=existing_of_task.id,
                                    motion_id=motion_id,
                                    workspace=ws_name,
                                    project=proj_name,
                                    task_name=task_name
                                )
                                print(f"   📎 Auto-mapped existing: '{task_name}' OF:{existing_of_task.id[:8]}... → Motion:{motion_id[:8]}...")

                            # Check completion sync for this newly-mapped pair
                            if motion_completed and not existing_of_task.completed:
                                reverse_sync_plan['of_tasks_to_complete'].append({
                                    'of_id': existing_of_task.id,
                                    'motion_id': motion_id,
                                    'task_name': task_name,
                                    'workspace': ws_name,
                                    'project': proj_name
                                })
                        elif not motion_completed:
                            # Truly new Motion-only task → create in OmniFocus
                            of_folder = reverse_ws_mapping.get(ws_name, ws_name)
                            if proj_name in of_projects_by_folder.get(of_folder, set()):
                                reverse_sync_plan['of_tasks_to_create'].append({
                                    'motion_id': motion_id,
                                    'task_name': task_name,
                                    'workspace': ws_name,
                                    'project': proj_name,
                                    'description': motion_task.get('description', ''),
                                    'due_date': motion_task.get('dueDate'),
                                    'priority': motion_task.get('priority', 'MEDIUM'),
                                    'duration': motion_task.get('duration'),
                                })
                        continue

                    # Get the actual OmniFocus task
                    of_task = of_tasks_by_id.get(of_id)

                    if not of_task:
                        # Mapping exists but task not found in OmniFocus (maybe deleted)
                        continue

                    # Check if Motion task is completed but OmniFocus task isn't
                    if motion_completed and not of_task.completed:
                        reverse_sync_plan['of_tasks_to_complete'].append({
                            'of_id': of_id,
                            'motion_id': motion_id,
                            'task_name': task_name,
                            'workspace': ws_name,
                            'project': proj_name
                        })

        print(f"   📊 Tasks to complete in OmniFocus: {len(reverse_sync_plan['of_tasks_to_complete'])}")
        print(f"   📊 Tasks to create in OmniFocus: {len(reverse_sync_plan['of_tasks_to_create'])}")
        return reverse_sync_plan
    
    def execute_reverse_sync_plan(self, reverse_sync_plan: Dict, motion_data: Optional[Dict] = None) -> bool:
        """Execute Motion → OmniFocus sync plan."""
        print("⚡ Executing reverse sync plan...")

        completed_count = 0
        created_count = 0
        failed_count = 0

        # Use passed motion_data to preserve auto-mappings and refreshed statuses
        if motion_data is None:
            motion_data = self.load_motion_data_from_file()

        # Complete tasks
        for task_data in reverse_sync_plan['of_tasks_to_complete']:
            task_name = task_data['task_name']
            of_id = task_data['of_id']

            print(f"   🔄 Completing OF task: '{task_name}'...")

            success = OmniFocusManager.complete_task(of_id)

            if success:
                completed_count += 1
                print(f"      ✅ Completed in OmniFocus")
            else:
                failed_count += 1

        # Create new tasks in OmniFocus
        for task_data in reverse_sync_plan['of_tasks_to_create']:
            task_name = task_data['task_name']
            proj_name = task_data['project']
            motion_id = task_data['motion_id']
            ws_name = task_data['workspace']

            print(f"   ➕ Creating OF task: '{task_name}' in '{proj_name}'...")

            # Build note with Motion link
            ws_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('id', '')
            proj_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('id', '')
            motion_url = f"https://app.usemotion.com/web/pm/workspaces/{ws_id}/projects/{proj_id}/views/default?task={motion_id}"

            desc_body = strip_html(extract_body(task_data.get('description', '') or ''))
            note = build_description(body=desc_body, motion_url=motion_url)

            # Map Motion priority to OF flagged
            flagged = task_data.get('priority') == 'ASAP'

            # Normalize due_date to YYYY-MM-DD (Motion returns full ISO datetime)
            raw_due = task_data.get('due_date') or ''
            of_due_date = raw_due[:10] if raw_due else None

            new_of_id = OmniFocusManager.create_task(
                project_name=proj_name,
                task_name=task_name,
                note=note,
                due_date=of_due_date,
                flagged=flagged,
                estimated_minutes=task_data.get('duration')
            )

            if new_of_id:
                created_count += 1
                print(f"      ✅ Created in OmniFocus (ID: {new_of_id[:8]}...)")

                # Store bidirectional mapping
                if self.id_mapper:
                    self.id_mapper.add_mapping(
                        of_id=new_of_id,
                        motion_id=motion_id,
                        workspace=ws_name,
                        project=proj_name,
                        task_name=task_name
                    )

                # Add OF link to Motion task description
                motion_task = self._find_motion_task_in_data(motion_data, motion_id)
                if motion_task:
                    desc = motion_task.get('description', '') or ''
                    of_url = f"omnifocus:///task/{new_of_id}"
                    if 'omnifocus:///' not in desc.lower():
                        body = extract_body(desc)
                        new_desc = build_description(body=body, of_url=of_url)
                        MotionSync.update_task(motion_id, {'description': new_desc})
                        time.sleep(self.config.api_rate_limit_delay)
            else:
                failed_count += 1

        # Always save - auto-mappings from create_reverse_sync_plan need persisting
        has_changes = created_count > 0 or completed_count > 0
        self.save_motion_data_to_file(motion_data, update_timestamp=has_changes)

        print(f"\n📊 Reverse Sync Results:")
        print(f"   ✅ Completed: {completed_count}")
        print(f"   ➕ Created: {created_count}")
        print(f"   ❌ Failed: {failed_count}")

        return failed_count == 0

    def perform_sync_comparison_from_structure(self, motion_data: Dict):
        """Perform sync comparison using the loaded OmniFocus structure."""
        print("🔍 Performing sync comparison from OF structure...")
        try:
            sync_plan = self.create_sync_plan_from_structure(motion_data)
            self.execute_sync_plan(sync_plan, motion_data)
        except Exception as e:
            print(f"❌ Error during sync comparison: {e}")
            traceback.print_exc()

    def create_sync_plan_from_structure(self, motion_data: Dict) -> Dict:
        """Create a sync plan by comparing individual task modification times."""
        print(" Creating sync plan from OF structure...")
        self.last_sync_timestamp = motion_data.get("metadata", {}).get("last_sync_timestamp")
        if self.last_sync_timestamp:
            print(f"ℹ️  Comparing against last sync time: {self.last_sync_timestamp}")

        # Use ignored folders from config
        ignore_folders = self.ignored_folders
        sync_plan = {
            'workspaces_to_create': [], 'projects_to_create': [], 'tasks_to_create': [],
            'tasks_to_update': [], 'tasks_to_complete': []
        }
        motion_workspaces = motion_data.get('workspaces', {})

        for folder in self.of_structure:
            folder_name = folder.name.strip()
            if folder_name in ignore_folders: continue

            # ✅ Map OmniFocus folder to Motion workspace (or use same name)
            motion_workspace_name = self.workspace_mapping.get(folder_name, folder_name)

            if motion_workspace_name not in motion_workspaces:
                print(f"⚠️  Warning: No Motion workspace found for OF folder '{folder_name}'")
                print(f"   → Tried to find: '{motion_workspace_name}'")
                print(f"   → Run 'python3 list_motion_workspaces.py' and update workspace_mapping")
                sync_plan['workspaces_to_create'].append(folder_name)
                continue
            
            workspace_projects = motion_workspaces[motion_workspace_name].get('projects', {})

            for project in folder.projects:
                project_name = project.name.strip()

                if project_name not in workspace_projects:
                    if not project.completed:
                        sync_plan['projects_to_create'].append({
                            'name': project_name, 
                            'workspace': folder_name,  # Original OF folder name
                            'motion_workspace': motion_workspace_name  # Mapped Motion workspace
                        })
                    continue

                project_tasks = workspace_projects[project_name].get('tasks', {})
                for task in project.tasks:
                    task_name = task.name.strip()
                    motion_task_exists = task_name in project_tasks

                    if motion_task_exists:
                        motion_task = project_tasks[task_name]
                        is_completed_in_motion = (motion_task.get('status') or {}).get('name', '').lower() == 'completed'

                        if task.completed and not is_completed_in_motion:
                            # Deduplicate: skip if this Motion task ID is already queued for completion
                            motion_id = motion_task['id']
                            already_queued = any(t['motion_task_id'] == motion_id for t in sync_plan['tasks_to_complete'])
                            if not already_queued:
                                sync_plan['tasks_to_complete'].append({
                                    'motion_task_id': motion_id,
                                    'name': task_name,
                                    'workspace': motion_workspace_name,
                                    'project': project_name
                                })
                        
                        elif not task.completed:
                            of_mod_time = task.of_modification_date

                            # Only check for updates if OF task was modified after last sync
                            if (not self.last_sync_timestamp) or (of_mod_time and of_mod_time > self.last_sync_timestamp):
                                self._check_task_for_updates(task, motion_task, sync_plan, project_name, motion_workspace_name)

                    elif not task.completed:
                        sync_plan['tasks_to_create'].append({
                            'name': task_name, 'project': project_name, 'workspace': folder_name,
                            'of_id': task.id,  # ✅ Include OmniFocus ID for mapping
                            'due_date': getattr(task, 'due_date', None),
                            'defer_date': getattr(task, 'defer_date', None),  # ✅ NEW: Start date
                            'duration_minutes': getattr(task, 'duration_minutes', None),
                            'flagged': getattr(task, 'flagged', False),
                            'of_priority': getattr(task, 'of_priority', None),  # ✅ NEW: Priority tag
                            'note': getattr(task, 'note', None),
                            'url': getattr(task, 'url', None),
                            'contexts': getattr(task, 'contexts', []),  # ✅ NEW: Tags
                            'repeat_rule': getattr(task, 'repeat_rule', None)  # ✅ NEW: Recurrence
                        })

        print(f"\n📋 Sync Plan Summary:")
        for key, value in sync_plan.items(): print(f"   {key.replace('_', ' ').title()}: {len(value)}")
        return sync_plan
    
    def _check_task_for_updates(self, of_task: OFTask, motion_task: Dict, sync_plan: Dict, project_name: str, workspace_name: str):
        """Check if an existing ACTIVE Motion task needs updates based on OmniFocus data."""
        updates_needed, field_changes = {}, []

        # Due date: normalize both to YYYY-MM-DD
        of_due_date = getattr(of_task, 'due_date', None) or ""
        motion_due = (motion_task.get('dueDate') or "")[:10]
        if of_due_date != motion_due:
            updates_needed['dueDate'] = of_due_date or None
            field_changes.append("due date")

        # Duration: normalize None/0/"NONE" to comparable values
        of_duration = getattr(of_task, 'duration_minutes', None)
        motion_duration = motion_task.get('duration')
        # Treat None, 0, and "NONE" as equivalent (no duration set)
        of_dur_normalized = of_duration if of_duration and of_duration != "NONE" else None
        motion_dur_normalized = motion_duration if motion_duration and motion_duration != "NONE" else None
        if of_dur_normalized != motion_dur_normalized:
            updates_needed['duration'] = of_dur_normalized
            field_changes.append("duration")

        # Priority
        of_priority = self.map_of_priority_to_motion(of_task)
        motion_priority = (motion_task.get('priority') or "MEDIUM").upper()
        if of_priority != motion_priority:
            updates_needed['priority'] = of_priority
            field_changes.append("priority")

        # Description: compare body only (strip metadata from both sides)
        of_note_body = extract_body(getattr(of_task, 'note', None) or '')
        motion_desc_body = extract_body(motion_task.get('description', '') or '')
        if of_note_body != motion_desc_body:
            of_tags = getattr(of_task, 'contexts', []) or []
            of_url = getattr(of_task, 'url', None)
            updates_needed['description'] = build_description(
                body=of_note_body, tags=of_tags if of_tags else None, of_url=of_url
            )
            field_changes.append("description")

        if updates_needed:
            task_name = motion_task.get('name', 'Unknown Task')
            print(f"   ↪️  Planning update for '{task_name}' ({', '.join(field_changes)})")
            sync_plan['tasks_to_update'].append({
                'motion_task_id': motion_task['id'], 
                'name': task_name, 
                'updates': updates_needed,
                'workspace': workspace_name,
                'project': project_name
            })

    def execute_sync_plan(self, sync_plan: Dict, motion_data: Dict) -> bool:
        """Execute the sync plan by creating/updating items in Motion."""
        print("⚡ Executing sync plan...")
        try:
            # ❌ REMOVED: Motion API doesn't support creating workspaces
            # Workspaces must be created manually in Motion UI
            for workspace_name in sync_plan['workspaces_to_create']:
                print(f"⚠️  Workspace '{workspace_name}' doesn't exist in Motion.")
                print(f"   → Please create it manually in Motion, or map it to an existing workspace.")
                # Skip creating projects for non-existent workspaces
                continue
            
            for project_data in sync_plan['projects_to_create']:
                project_name = project_data['name']
                of_folder_name = project_data['workspace']  # Original OmniFocus folder name
                motion_workspace_name = project_data.get('motion_workspace', of_folder_name)  # Mapped Motion workspace
                
                if motion_workspace_name in motion_data['workspaces']:
                    workspace_id = motion_data['workspaces'][motion_workspace_name]['id']
                    created_proj = MotionSync.create_project_in_workspace(workspace_id, project_name)
                    if created_proj and created_proj.get('id'):
                        # Store under Motion workspace name
                        motion_data['workspaces'][motion_workspace_name]['projects'][project_name] = {'id': created_proj['id'], 'tasks': {}}
                        print(f"   ✅ Created project '{project_name}' in Motion workspace '{motion_workspace_name}'")
                else:
                    print(f"   ⚠️  Skipping project '{project_name}' - workspace '{motion_workspace_name}' not found")

            for task_data in sync_plan['tasks_to_create']:
                task_name = task_data['name']
                project_name = task_data['project']
                of_folder_name = task_data['workspace']  # Original OmniFocus folder
                
                # Map to Motion workspace
                motion_workspace_name = self.workspace_mapping.get(of_folder_name, of_folder_name)
                
                if motion_workspace_name in motion_data['workspaces'] and project_name in motion_data['workspaces'][motion_workspace_name]['projects']:
                    project_id = motion_data['workspaces'][motion_workspace_name]['projects'][project_name]['id']
                    
                    # ✅ Get OmniFocus task ID for mapping
                    of_task_id = task_data.get('of_id')
                    
                    created_task = self._create_task(task_name, project_id, task_data, motion_data, motion_workspace_name)
                    if created_task:
                        motion_data['workspaces'][motion_workspace_name]['projects'][project_name]['tasks'][task_name] = created_task
                        
                        # ✅ Store bidirectional ID mapping
                        if of_task_id and created_task.get('id') and self.id_mapper:
                            self.id_mapper.add_mapping(
                                of_id=of_task_id,
                                motion_id=created_task['id'],
                                workspace=motion_workspace_name,  # Use Motion workspace name
                                project=project_name,
                                task_name=task_name
                            )
                            print(f"      📎 Mapped OF:{of_task_id[:8]}... → Motion:{created_task['id'][:8]}...")

                            # Write Motion link back to OmniFocus notes
                            workspace_id = motion_data['workspaces'][motion_workspace_name]['id']
                            motion_url = f"https://app.usemotion.com/web/pm/workspaces/{workspace_id}/projects/{project_id}/views/default?task={created_task['id']}"
                            of_note_body = extract_body(task_data.get('note', '') or '')
                            new_note = build_description(
                                body=of_note_body,
                                motion_url=motion_url
                            )
                            OmniFocusManager.update_task(of_task_id, {'note': new_note})

            for task_data in sync_plan['tasks_to_update']:
                updated_task_response = MotionSync.update_task(task_data['motion_task_id'], task_data['updates'])
                if updated_task_response:
                    workspace_name = task_data['workspace']
                    project_name = task_data['project']
                    task_name = task_data['name']
                    if workspace_name in motion_data['workspaces'] and project_name in motion_data['workspaces'][workspace_name]['projects']:
                        motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks'][task_name] = updated_task_response

            for task_data in sync_plan['tasks_to_complete']:
                success = MotionSync.complete_task(task_data['motion_task_id'])
                if success:
                    workspace_name = task_data['workspace']
                    project_name = task_data['project']
                    task_name = task_data['name']
                    if workspace_name in motion_data['workspaces'] and \
                       project_name in motion_data['workspaces'][workspace_name]['projects'] and \
                       task_name in motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks']:
                        
                        motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks'][task_name]['status'] = {'name': 'Completed', 'isResolvedStatus': True}
                        motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks'][task_name]['updatedTime'] = datetime.now(timezone.utc).isoformat()

            # Backfill Motion URLs into OF notes for mapped tasks missing them
            self._backfill_missing_cross_links(motion_data)

            self.save_motion_data_to_file(motion_data, update_timestamp=True)
            print("✅ Sync plan execution completed")
            return True
        except Exception as e:
            print(f"❌ Error executing sync plan: {e}")
            traceback.print_exc()
            return False

    def _create_task(self, name: str, project_id: str, task_data: Dict, motion_data: Dict, motion_workspace_name: str) -> Optional[Dict]:
        """Create a task in Motion and return the full task object."""
        workspace_id = motion_data['workspaces'][motion_workspace_name]['id']
        schedule_name = self.workspace_schedule_mapping.get(motion_workspace_name)
        
        # ✅ NEW: Determine priority from OF task data
        # Create a temp OFTask object to use priority mapping function
        temp_task = OFTask(
            id="", name="", 
            flagged=task_data.get('flagged', False),
            of_priority=task_data.get('of_priority')
        )
        motion_priority = self.map_of_priority_to_motion(temp_task)
        
        result = MotionSync.create_task_in_project(
            project_id=project_id, workspace_id=workspace_id, task_name=name,
            description=task_data.get('note'), of_task_url=task_data.get('url'),
            due_date_str=task_data.get('due_date'),
            defer_date_str=task_data.get('defer_date'),  # ✅ NEW: Start date
            task_duration=task_data.get('duration_minutes'),
            schedule_name_to_use=schedule_name,
            priority=motion_priority,  # ✅ NEW: Use proper priority mapping
            labels=task_data.get('contexts', [])  # ✅ NEW: Tags/contexts
        )
        return result if result and result.get('id') else None

    def _find_motion_task_in_data(self, motion_data: Dict, motion_id: str) -> Optional[Dict]:
        """Find a Motion task by ID in the nested data structure."""
        for ws_data in motion_data.get('workspaces', {}).values():
            for proj_data in ws_data.get('projects', {}).values():
                for task_data in proj_data.get('tasks', {}).values():
                    if task_data.get('id') == motion_id:
                        return task_data
        return None

    def _backfill_missing_cross_links(self, motion_data: Dict):
        """Add Motion URLs to OF notes for mapped tasks that are missing them.

        Runs during normal sync to ensure all mapped tasks have cross-links,
        not just newly created ones.
        """
        if not self.id_mapper:
            return

        mappings = self.id_mapper.state_data.get('task_mappings', {})
        if not mappings:
            return

        # Build lookup dict for O(1) access by task ID
        of_tasks_by_id = {}
        if self.of_structure:
            for folder in self.of_structure:
                for project in folder.projects:
                    for task in project.tasks:
                        of_tasks_by_id[task.id] = task

        of_updated = 0

        for mapping in mappings.values():
            of_id = mapping.get('of_id')
            motion_id = mapping.get('motion_id')
            if not of_id or not motion_id:
                continue

            ws_name = mapping.get('workspace', '')
            proj_name = mapping.get('project', '')

            of_task = of_tasks_by_id.get(of_id)

            if not of_task:
                continue

            note = of_task.note or ''
            if 'app.usemotion.com' in note:
                continue  # Already has Motion URL

            # Build and write the Motion URL
            ws_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('id', '')
            proj_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('id', '')
            if not ws_id or not proj_id:
                continue

            motion_url = f"https://app.usemotion.com/web/pm/workspaces/{ws_id}/projects/{proj_id}/views/default?task={motion_id}"
            body = extract_body(note)
            new_note = build_description(body=body, motion_url=motion_url)
            if OmniFocusManager.update_task(of_id, {'note': new_note}):
                of_updated += 1

        if of_updated > 0:
            print(f"   🔗 Added Motion URLs to {of_updated} OF task note(s)")

    def backfill_cross_links(self):
        """Backfill cross-reference links between OmniFocus and Motion tasks."""
        motion_data = self.load_motion_data_from_file()

        mappings = self.id_mapper.state_data.get('task_mappings', {}) if self.id_mapper else {}
        if not mappings:
            print("ℹ️  No task mappings found. Skipping cross-link backfill.")
            return

        print(f"\n🔗 Backfilling cross-links for {len(mappings)} mapped tasks...")

        # Reuse OF structure if already loaded
        of_structure = self.of_structure or self.load_omnifocus_structure()
        of_tasks_by_id = {}
        for folder in of_structure:
            for project in folder.projects:
                for task in project.tasks:
                    of_tasks_by_id[task.id] = task

        motion_updated = 0
        of_updated = 0

        for mapping in mappings.values():
            of_id = mapping.get('of_id')
            motion_id = mapping.get('motion_id')
            if not of_id or not motion_id:
                continue

            ws_name = mapping.get('workspace', '')
            proj_name = mapping.get('project', '')
            ws_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('id', '')
            proj_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('id', '')

            # 1. Add OF link to Motion task description if missing
            motion_task = self._find_motion_task_in_data(motion_data, motion_id)
            if motion_task:
                desc = motion_task.get('description', '') or ''
                if 'omnifocus:///' not in desc.lower():
                    of_url = f"omnifocus:///task/{of_id}"
                    body = extract_body(desc)
                    new_desc = build_description(body=body, of_url=of_url)
                    result = MotionSync.update_task(motion_id, {'description': new_desc})
                    if result:
                        motion_updated += 1
                    time.sleep(self.config.api_rate_limit_delay)

            # 2. Add Motion link to OF task note if missing
            of_task = of_tasks_by_id.get(of_id)
            if of_task:
                note = of_task.note or ''
                if 'app.usemotion.com' not in note:
                    motion_url = f"https://app.usemotion.com/web/pm/workspaces/{ws_id}/projects/{proj_id}/views/default?task={motion_id}"
                    body = extract_body(note)
                    new_note = build_description(body=body, motion_url=motion_url)
                    OmniFocusManager.update_task(of_id, {'note': new_note})
                    of_updated += 1

        print(f"🔗 Cross-link backfill complete: {motion_updated} Motion tasks, {of_updated} OF tasks updated")

    def log_workspace_schedule_info(self):
        """Log workspace schedule mapping for reference."""
        print(" M-> Workspace Schedule Mapping (for reference):")
        for workspace, schedule in self.workspace_schedule_mapping.items():
            print(f"    • {workspace} → {schedule}")
        print(" M-> Note: Motion automatically assigns workspace schedules to new items")

    def run_workflow(self, refresh_mapping: bool = False, sync_only: bool = False):
        """Main workflow that combines mapping and sync."""
        print("🚀 OmniFocus to Motion Hybrid Sync")
        print("=" * 50)
        
        if refresh_mapping or not self.check_local_data_exists():
            print("📊 Creating/Refreshing Motion data mapping...")
            self.create_comprehensive_mapping()
        
        self.log_workspace_schedule_info()
        
        if not sync_only:
            print("🔄 Starting synchronization...")
            self.sync_omnifocus_to_motion()
        
        print("✅ Workflow completed successfully!")
    
    def run_bidirectional_sync(self, refresh_mapping: bool = False):
        """
        Execute full bidirectional synchronization.
        Loads OmniFocus structure once and shares it across all phases.
        Phase 1: OmniFocus → Motion
        Phase 2: Motion → OmniFocus
        """
        print("🚀 OmniFocus ↔ Motion Bidirectional Sync")
        print("=" * 50)

        if refresh_mapping or not self.check_local_data_exists():
            print("📊 Creating/Refreshing Motion data mapping...")
            self.create_comprehensive_mapping()

        # Load OmniFocus structure once for the entire sync
        self.of_structure = self.load_omnifocus_structure()
        if not self.of_structure:
            print("❌ Failed to load OmniFocus structure. Cannot proceed.")
            return

        if refresh_mapping:
            print("\n" + "=" * 50)
            print("Phase 0: Backfill Cross-Links")
            print("=" * 50)
            self.backfill_cross_links()

        self.log_workspace_schedule_info()

        print("\n" + "=" * 50)
        print("Phase 1: OmniFocus → Motion")
        print("=" * 50)
        self.sync_omnifocus_to_motion()

        print("\n" + "=" * 50)
        print("Phase 2: Motion → OmniFocus")
        print("=" * 50)
        self.sync_motion_to_omnifocus()

        print("\n✅ Bidirectional sync completed successfully!")

def main():
    """Main function with command line options."""
    # Initialize the lock immediately at startup
    lock_handle = acquire_lock()
    
    
    parser = argparse.ArgumentParser(description="OmniFocus to Motion Sync v3 Stable")
    parser.add_argument("--refresh-mapping", action="store_true", help="Force refresh of Motion data mapping and then sync")
    parser.add_argument("--sync-only", action="store_true", help="Only run sync, skip mapping creation (uses existing local data)")
    parser.add_argument("--mapping-only", action="store_true", help="Only create/refresh the local mapping file, skip sync")
    parser.add_argument("--config", type=str, default="config.json", help="Path to configuration file (default: config.json)")

    args = parser.parse_args()
    
    # Load configuration
    config = Config(args.config)
    
    api_key = os.getenv("MOTION_API_KEY")
    if not api_key:
        api_key = input("Enter your Motion API key: ").strip()
        if not api_key:
            print("❌ No API key provided. Exiting.")
            return
    
    MotionSync.set_api_key(api_key)
    sync = MotionHybridSync(api_key, config)
    
    try:
        if args.mapping_only:
            print("📊 Running mapping creation only...")
            sync.create_comprehensive_mapping()
        elif args.sync_only:
            print("🔄 Running sync only...")
            sync.sync_omnifocus_to_motion()
        else:
            sync.run_bidirectional_sync(refresh_mapping=args.refresh_mapping)
            
    except KeyboardInterrupt:
        print("\n⏹️ Process interrupted by user")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
