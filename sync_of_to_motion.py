#!/usr/bin/env python3
"""
OmniFocus to Motion Sync Script
Version: 4.0 - Sprint 4 (Polish, Hardening & Optimization)

Bidirectional sync between OmniFocus and Motion with:
- ID-based task matching
- Sequential project support
- Structured logging with correlation IDs
- State backups with SHA-256 checksums
- Dry-run mode
- Sync history tracking

Usage: python3 sync_of_to_motion.py [--refresh-mapping] [--sync-only] [--mapping-only] [--dry-run] [--config FILE]
"""

import os
import sys
import json
import time
import re
import html as html_mod
import uuid
import hashlib
import shutil
import logging
import logging.handlers
import argparse
import subprocess
import fcntl
import traceback
import atexit
import requests
from typing import Dict, List, Optional, Union, Any
from datetime import datetime, timedelta, timezone

# --- Global Configuration ---
LOCK_FILE = "/tmp/of2motion.lock"
DEFAULT_CONFIG_FILE = "config.json"
METADATA_SEPARATOR = "═" * 40
LOG_DIR = os.path.expanduser("~/Library/Logs/OmniFocusMotionSync")
STATE_FILE = "motion_hierarchical_mapping.json"

# --- Logging Setup ---
logger = logging.getLogger("OFMotionSync")


def setup_logging(log_dir: str = LOG_DIR, console_level: int = logging.INFO):
    """Configure structured logging with file rotation and console output."""
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "sync.log")

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # Prevent duplicate handlers on re-initialization

    # File handler with rotation (10MB, 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    # Console handler (preserves current UX)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


SYNC_HISTORY_FILE = "sync_history.jsonl"


def append_sync_history(run_id: str, duration_seconds: float, dry_run: bool,
                        forward_stats: Dict, reverse_stats: Dict):
    """Append one JSON line per sync run to the history file."""
    record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'run_id': run_id,
        'duration_seconds': round(duration_seconds, 2),
        'dry_run': dry_run,
        'forward': forward_stats,
        'reverse': reverse_stats,
    }
    try:
        with open(SYNC_HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except IOError as e:
        logger.warning(f"⚠️  Could not write sync history: {e}")


def build_description(body: str = "", tags: Optional[List[str]] = None,
                      of_url: Optional[str] = None, motion_url: Optional[str] = None,
                      sequence_info: Optional[str] = None) -> str:
    """Build a standardized description/note with metadata below a separator."""
    parts = []
    if body:
        parts.append(body.strip())

    metadata = []
    if sequence_info:
        metadata.append(sequence_info)
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
    """Strip HTML tags and decode entities from text, converting <br> and </p> to newlines."""
    if not text or '<' not in text:
        return text or ""
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>\s*<p>', '\n\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_mod.unescape(text)
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
                logger.info(f"✅ Loaded configuration from {self.config_file}")
                return config_data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"⚠️  Error reading config file: {e}. Using defaults.")
        else:
            logger.warning(f"⚠️  Config file '{self.config_file}' not found. Using defaults.")
        
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
            logger.info(f"✅ Configuration saved to {self.config_file}")
            return True
        except IOError as e:
            logger.error(f"❌ Error saving config: {e}")
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

    @property
    def default_due_date_offset_days(self) -> int:
        """Default number of days to set as due date when none provided."""
        return self.config.get("sync_settings", {}).get("default_due_date_offset_days", 14)

    @property
    def lock_file(self) -> str:
        """Path to lock file."""
        return self.config.get("paths", {}).get("lock_file", LOCK_FILE)

    @property
    def state_file(self) -> str:
        """Path to state file."""
        return self.config.get("paths", {}).get("state_file", STATE_FILE)

    @property
    def log_directory(self) -> str:
        """Path to log directory."""
        return os.path.expanduser(self.config.get("paths", {}).get("log_directory", LOG_DIR))

    @property
    def sequential_project_handling(self) -> Dict:
        """Get sequential project handling settings."""
        return self.config.get("sequential_project_handling", {
            "enabled": True
        })

def acquire_lock(lock_file: str = LOCK_FILE):
    """Ensures only one instance of the script runs at a time."""
    # We use a persistent handle so the lock stays active for the script's life
    fp = open(lock_file, 'w')
    try:
        # LOCK_EX: Exclusive lock
        # LOCK_NB: Non-blocking (fail immediately if locked)
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Ensure lock is released on exit
        atexit.register(lambda: (fp.close(), os.remove(lock_file) if os.path.exists(lock_file) else None))

        return fp
    except IOError:
        fp.close()  # Close file handle if lock fails
        logger.error("⛔ Another instance is already running. Exiting to prevent API conflicts.")
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
    def __init__(self, id, name, url=None, of_modification_date=None, completed=False, tasks=None, sequential=False):
        self.id = id
        self.name = name
        self.url = url
        self.of_modification_date = of_modification_date
        self.completed = completed
        self.tasks = tasks or []
        self.sequential = sequential

class OFFolder:
    """OmniFocus folder data structure."""
    def __init__(self, id, name, projects=None):
        self.id = id
        self.name = name
        self.projects = projects or []

# --- Sentinel for 404 responses (falsy, but distinguishable from None) ---
class _NotFound:
    """Sentinel returned by _make_request for 404 responses.
    Falsy so existing `if not result:` checks still work,
    but distinguishable via `result is NOT_FOUND`."""
    def __bool__(self): return False
    def __repr__(self): return 'NOT_FOUND'

NOT_FOUND = _NotFound()

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
                    logger.error(f"❌ Rate limited {cls.MAX_RETRIES} times on {method} {endpoint}. Giving up.")
                    return None
                try:
                    retry_after = int(response.headers.get("Retry-After", 60))
                except (ValueError, TypeError):
                    retry_after = 60
                logger.info(f"⏳ Rate limited. Waiting {retry_after} seconds... (attempt {_retry_count + 1}/{cls.MAX_RETRIES})")
                time.sleep(retry_after)
                return cls._make_request(method, endpoint, json_data, params, _retry_count=_retry_count + 1)
            elif response.status_code == 404:
                logger.debug(f"🔍 404 Not Found on {method} {endpoint}")
                return NOT_FOUND
            else:
                logger.error(f"❌ HTTP {response.status_code} on {method} {endpoint}: {response.text}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Request error on {method} {endpoint}: {e}")
            return None

    @classmethod
    def create_project_in_workspace(cls, workspace_id: str, project_name: str, of_project_url: Optional[str] = None) -> Optional[Dict]:
        logger.debug(f" M-> Creating project: {project_name} in workspace {workspace_id}...")
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
                               labels: Optional[List[str]] = None,
                               default_due_date_offset: int = 14,
                               sequence_info: Optional[str] = None) -> Optional[Dict]:
        payload = {
            "name": task_name, "projectId": project_id,
            "workspaceId": workspace_id, "priority": priority
        }

        desc = build_description(
            body=description or "",
            tags=labels if labels and isinstance(labels, list) else None,
            of_url=of_task_url,
            sequence_info=sequence_info
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
                payload["dueDate"] = (datetime.now() + timedelta(days=default_due_date_offset)).strftime("%Y-%m-%d")

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
            logger.debug(f" M-> Info: No valid fields to update for task {task_id}. Skipping.")
            return {"id": task_id, **updates}

        logger.debug(f"    M-> Sending update for task {task_id} with payload: {payload}")
        response = cls._make_request("PATCH", f"tasks/{task_id}", json_data=payload)

        if response and response is not NOT_FOUND:
            logger.debug(f" M-> ✨ Successfully updated task (ID: {task_id})")
            return response
        else:
            logger.error(f"🚨 Error updating task {task_id}. API returned {'404' if response is NOT_FOUND else 'error'}.")
            return None

    @classmethod
    def complete_task(cls, task_id: str) -> bool:
        """Mark a task as completed in Motion. Returns True on success."""
        logger.debug(f" M-> Completing task (ID: {task_id})")
        response = cls._make_request('PATCH', f'tasks/{task_id}', json_data={'status': 'completed'})
        if response and response is not NOT_FOUND:
            logger.debug(f"     M-> ✨ Completed task successfully.")
            return True
        logger.error(f"     M-> ❌ Failed to complete task ({'404 - task not found' if response is NOT_FOUND else 'API error'}).")
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
                logger.warning(f"      ⚠️  OmniFocus task {task_id[:8]}... not found")
                return False
            elif output == 'omnifocus_not_running':
                logger.error(f"      ❌ OmniFocus is not running")
                return False
            elif output.startswith('error:'):
                logger.error(f"      ❌ OmniFocus error: {output[6:]}")
                return False
            else:
                error_msg = result.stderr.strip() or output
                logger.error(f"      ❌ OmniFocus error: {error_msg}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"      ❌ OmniFocus script timeout")
            return False
        except Exception as e:
            logger.error(f"      ❌ Failed to complete OmniFocus task: {e}")
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
                logger.error(f"      ❌ OmniFocus update error: {error_msg}")
                return False

        except Exception as e:
            logger.error(f"      ❌ Failed to update OmniFocus task: {e}")
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
                logger.error(f"      ❌ OmniFocus is not running")
                return None
            elif output == 'project_not_found':
                logger.warning(f"      ⚠️  OmniFocus project '{project_name}' not found")
                return None
            elif output and not output.startswith('error'):
                return output  # This is the new task ID
            else:
                error_msg = result.stderr.strip() or output
                logger.error(f"      ❌ OmniFocus create error: {error_msg}")
                return None

        except subprocess.TimeoutExpired:
            logger.error(f"      ❌ OmniFocus script timeout")
            return None
        except Exception as e:
            logger.error(f"      ❌ Failed to create OmniFocus task: {e}")
            return None

# --- Task ID Mapping Manager ---
class TaskIDMapper:
    """Manages bidirectional mapping between OmniFocus and Motion task IDs."""

    def __init__(self, state_data: Dict):
        self.state_data = state_data
        if 'task_mappings' not in self.state_data:
            self.state_data['task_mappings'] = {}
        self._rebuild_indexes()

    def _rebuild_indexes(self):
        """Build reverse lookup dicts for O(1) access."""
        self._of_to_motion = {}
        self._motion_to_of = {}
        for mapping in self.state_data['task_mappings'].values():
            of_id = mapping.get('of_id')
            motion_id = mapping.get('motion_id')
            if of_id:
                self._of_to_motion[of_id] = motion_id
            if motion_id:
                self._motion_to_of[motion_id] = of_id

    def add_mapping(self, of_id: str, motion_id: str, workspace: str, project: str, task_name: str,
                    sequence_info: Optional[Dict] = None):
        """Store bidirectional ID mapping with optional sequence metadata."""
        # Clean up any old mapping for these IDs to prevent ghost entries
        old_motion = self._of_to_motion.get(of_id)
        if old_motion and old_motion != motion_id:
            old_key = self._create_key(of_id, old_motion)
            self.state_data['task_mappings'].pop(old_key, None)
            self._motion_to_of.pop(old_motion, None)

        old_of = self._motion_to_of.get(motion_id)
        if old_of and old_of != of_id:
            old_key = self._create_key(old_of, motion_id)
            self.state_data['task_mappings'].pop(old_key, None)
            self._of_to_motion.pop(old_of, None)

        mapping_key = self._create_key(of_id, motion_id)
        mapping = {
            'of_id': of_id,
            'motion_id': motion_id,
            'workspace': workspace,
            'project': project,
            'task_name': task_name,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        if sequence_info:
            mapping.update(sequence_info)
        self.state_data['task_mappings'][mapping_key] = mapping
        # Maintain indexes
        self._of_to_motion[of_id] = motion_id
        self._motion_to_of[motion_id] = of_id

    def get_motion_id_from_of(self, of_id: str) -> Optional[str]:
        """Find Motion ID given OmniFocus ID. O(1) lookup."""
        return self._of_to_motion.get(of_id)

    def get_of_id_from_motion(self, motion_id: str) -> Optional[str]:
        """Find OmniFocus ID given Motion ID. O(1) lookup."""
        return self._motion_to_of.get(motion_id)

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
        self._rebuild_indexes()

    @staticmethod
    def _create_key(of_id: str, motion_id: str) -> str:
        """Create composite key for mapping."""
        return f"{of_id}::{motion_id}"


# --- State File Manager ---
class StateManager:
    """Manages state file persistence with backups and integrity checks."""

    STATE_VERSION = 2

    def __init__(self, filename: str = STATE_FILE):
        self.filename = filename
        self.backup_file = f"{filename}.backup"
        self.checksum_file = f"{filename}.sha256"

    def save(self, data: Dict, update_timestamp: bool = True):
        """Save state data with backup and checksum."""
        if 'metadata' not in data:
            data['metadata'] = {}

        data['metadata']['state_version'] = self.STATE_VERSION
        data['metadata']['total_workspaces'] = len(data.get('workspaces', {}))
        data['metadata']['total_projects'] = sum(
            len(ws.get("projects", {})) for ws in data.get('workspaces', {}).values()
        )
        data['metadata']['total_tasks'] = sum(
            sum(len(p.get("tasks", {})) for p in ws.get("projects", {}).values())
            for ws in data.get('workspaces', {}).values()
        )

        if update_timestamp:
            new_timestamp = datetime.now(timezone.utc).isoformat()
            data['metadata']['last_sync_timestamp'] = new_timestamp
            logger.info(f"💾 Saving with new sync timestamp: {new_timestamp}")

        # Backup current file before overwriting
        if os.path.exists(self.filename):
            try:
                shutil.copy2(self.filename, self.backup_file)
            except IOError as e:
                logger.warning(f"⚠️ Could not create backup: {e}")

        # Write the data
        try:
            json_str = json.dumps(data, indent=2, ensure_ascii=False)
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write(json_str)

            # Write checksum
            checksum = hashlib.sha256(json_str.encode('utf-8')).hexdigest()
            with open(self.checksum_file, 'w', encoding='utf-8') as f:
                f.write(checksum)

            logger.info(f"✅ State saved to {self.filename}")
        except IOError as e:
            logger.error(f"❌ Error saving state file: {e}")

    def load(self) -> Dict:
        """Load state data with integrity verification and backup restore."""
        if not os.path.exists(self.filename):
            logger.warning(f"⚠️ No state file found at '{self.filename}'. A new one will be created.")
            return {"metadata": {"state_version": self.STATE_VERSION}, "workspaces": {}, "task_mappings": {}}

        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                content = f.read()

            # Verify checksum if available
            if os.path.exists(self.checksum_file):
                with open(self.checksum_file, 'r', encoding='utf-8') as f:
                    expected_checksum = f.read().strip()
                actual_checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()
                if actual_checksum != expected_checksum:
                    logger.warning("⚠️ State file checksum mismatch — attempting backup restore")
                    return self._restore_from_backup()

            data = json.loads(content)
            if 'task_mappings' not in data:
                data['task_mappings'] = {}
            return data

        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"❌ Error reading state file: {e}")
            return self._restore_from_backup()

    def _restore_from_backup(self) -> Dict:
        """Attempt to restore state from backup file."""
        if not os.path.exists(self.backup_file):
            logger.error("❌ No backup file available. Starting with empty data.")
            return {"metadata": {"state_version": self.STATE_VERSION}, "workspaces": {}, "task_mappings": {}}

        try:
            with open(self.backup_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if 'task_mappings' not in data:
                data['task_mappings'] = {}
            logger.info("✅ Successfully restored state from backup")
            # Re-save to fix the primary file
            self.save(data, update_timestamp=False)
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"❌ Backup restore failed: {e}. Starting with empty data.")
            return {"metadata": {"state_version": self.STATE_VERSION}, "workspaces": {}, "task_mappings": {}}


# --- Main Sync Class ---
class MotionHybridSync:
    def __init__(self, api_key: str, config: Optional[Config] = None, dry_run: bool = False):
        self.api_key = api_key
        self.dry_run = dry_run

        # Load configuration (or use provided config object)
        self.config = config if config else Config()

        # Load mappings from config
        self.workspace_schedule_mapping = self.config.workspace_schedules
        self.workspace_mapping = self.config.workspace_mapping
        self.ignored_folders = self.config.ignored_folders

        self.last_sync_timestamp = None
        self.id_mapper = None  # Will be initialized when state is loaded
        self.of_structure = None  # Loaded lazily by run_bidirectional_sync or sync_omnifocus_to_motion
        self.state_manager = StateManager(self.config.state_file)

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
        return os.path.exists(self.config.state_file)
    
    def get_workspaces(self) -> Dict[str, Dict]:
        logger.debug("🔍 Fetching Motion workspaces...")
        # ✅ FIXED: Use MotionSync._make_request() instead of duplicate method
        data = MotionSync._make_request("GET", "workspaces")
        if not data or "workspaces" not in data:
            logger.error("❌ Failed to fetch workspaces")
            return {}
        workspaces = {w.get("name"): w for w in data["workspaces"] if w.get("name")}
        logger.info(f"✅ Found {len(workspaces)} workspaces")
        return workspaces
    
    def get_projects_for_workspace(self, workspace_id: str, workspace_name: str) -> Dict[str, Dict]:
        logger.debug(f"  📁 Fetching projects for workspace: {workspace_name}")
        projects, cursor = {}, None
        while True:
            params = {"workspaceId": workspace_id}
            if cursor: params["cursor"] = cursor
            data = MotionSync._make_request("GET", "projects", params=params)
            if not data or "projects" not in data: break
            for p in data["projects"]:
                if p_name := p.get("name"): projects[p_name] = p
            if not (cursor := data.get("meta", {}).get("nextCursor")): break
        logger.info(f"  📊 Total projects in {workspace_name}: {len(projects)}")
        return projects
    
    def get_tasks_for_project(self, project_id: str, project_name: str) -> Dict[str, Dict]:
        logger.debug(f"      Fetching tasks for project: {project_name}")
        tasks, cursor = {}, None
        while True:
            params = {"projectId": project_id}
            if cursor: params["cursor"] = cursor
            data = MotionSync._make_request("GET", "tasks", params=params)
            if not data or "tasks" not in data: break
            for t in data["tasks"]:
                if t_name := t.get("name"): tasks[t_name] = t
            if not (cursor := data.get("meta", {}).get("nextCursor")): break
        logger.debug(f"      📊 Total tasks in project '{project_name}': {len(tasks)}")
        return tasks
    
    def create_comprehensive_mapping(self) -> Dict:
        logger.info("🚀 Starting comprehensive Motion mapping creation...")
        workspaces = self.get_workspaces()
        if not workspaces: return {}

        # Preserve existing task_mappings when refreshing
        existing_data = self.load_motion_data_from_file()
        existing_mappings = existing_data.get('task_mappings', {})

        mapping_data = {"workspaces": {}, "task_mappings": existing_mappings}
        total_projects, total_tasks = 0, 0

        for ws_name, ws_data in workspaces.items():
            logger.info(f"\n🏢 Processing workspace: {ws_name}")
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

        logger.info(f"\n🎯 HIERARCHICAL MAPPING COMPLETE! Workspaces: {len(workspaces)}, Projects: {total_projects}, Tasks: {total_tasks}")
        if not self.dry_run:
            self.save_motion_data_to_file(mapping_data, update_timestamp=False)
        else:
            logger.info("[DRY RUN] Would save mapping data to file")
        return mapping_data

    def load_motion_data_from_file(self) -> Dict:
        """Loads the entire motion data structure from the local JSON file."""
        data = self.state_manager.load()
        self.id_mapper = TaskIDMapper(data)
        return data

    def save_motion_data_to_file(self, motion_data: Dict, update_timestamp: bool = True):
        """Save motion data using StateManager with backup and checksum."""
        self.state_manager.save(motion_data, update_timestamp=update_timestamp)

    def load_omnifocus_structure(self):
        """Load OmniFocus data structure using JXA script."""
        logger.info("📱 Loading OmniFocus structure...")
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
                let sequential = false;
                try { sequential = p.sequential(); } catch(e) {}
                const projectData = {
                    id: p.id(), name: p.name(), url: 'omnifocus:///project/' + p.id(),
                    modificationDate: p.modificationDate() ? p.modificationDate().toISOString() : null,
                    completed: p.completed(), sequential: sequential, tasks: []
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
                                
                                        // Detect priority tags (strip emojis/symbols, trim whitespace)
                                const tagClean = tagName.replace(/[^\p{L}\p{N}\s]/gu, '').trim().toLowerCase();
                                if (tagClean === 'high' || tagClean === 'high priority') ofPriority = 'high';
                                else if (tagClean === 'medium' || tagClean === 'medium priority') ofPriority = 'medium';
                                else if (tagClean === 'low' || tagClean === 'low priority') ofPriority = 'low';
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
                logger.error(f"❌ OmniFocus error: {raw_data['error']}")
                return []
            
            of_structure = []
            for fr_data in raw_data:
                folder = OFFolder(id=fr_data['id'], name=fr_data['name'])
                for pr_data in fr_data.get('projects', []):
                    project = OFProject(id=pr_data['id'], name=pr_data['name'], url=pr_data.get('url'),
                                        of_modification_date=pr_data.get('modificationDate'), completed=pr_data.get('completed', False),
                                        sequential=pr_data.get('sequential', False))
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
            
            logger.info(f"📱 Found {len(of_structure)} OF folders, {sum(len(f.projects) for f in of_structure)} projects, {sum(len(p.tasks) for f in of_structure for p in f.projects)} tasks.")
            return of_structure
        except Exception as e: 
            logger.error(f"🚨 Unexpected OF error: {e}")
            return []

    def sync_omnifocus_to_motion(self) -> Dict:
        """Real OmniFocus to Motion synchronization using local JSON data. Returns forward stats."""
        logger.info(" Starting OmniFocus to Motion synchronization...")
        local_data = self.load_motion_data_from_file()
        # Load OF structure only if not already loaded (standalone mode)
        if not self.of_structure:
            self.of_structure = self.load_omnifocus_structure()
        if not self.of_structure:
            logger.error("❌ Failed to load OmniFocus structure. Cannot proceed with sync.")
            return {'projects_created': 0, 'tasks_created': 0, 'tasks_updated': 0,
                    'tasks_completed': 0, 'failed': 0}
        forward_stats = self.perform_sync_comparison_from_structure(local_data)
        logger.info("✅ Synchronization completed!")
        return forward_stats

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
            logger.info("🔄 No tasks need completion status check")
            return motion_data

        logger.info(f"🔄 Checking {len(tasks_to_check)} task(s) for Motion completion status...")
        completed_count = 0
        stale_count = 0

        for mapping in tasks_to_check:
            motion_id = mapping.get('motion_id')
            ws_name = mapping.get('workspace', '')
            proj_name = mapping.get('project', '')
            task_name = mapping.get('task_name', '')

            # Fetch individual task status from Motion API
            task_data = MotionSync._make_request("GET", f"tasks/{motion_id}")
            if task_data is NOT_FOUND:
                # 404 — task was genuinely deleted in Motion.
                # Mark local copy as completed so we stop checking it.
                proj_tasks = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('tasks', {})
                if task_name in proj_tasks:
                    proj_tasks[task_name]['status'] = {'name': 'Completed', 'isResolvedStatus': True}
                    proj_tasks[task_name]['completed'] = True
                stale_count += 1
                logger.debug(f"   🗑️ Task '{task_name}' no longer exists in Motion (deleted/archived)")
                time.sleep(self.config.api_rate_limit_delay)
                continue
            if task_data is None:
                # Non-404 API error (500, timeout, etc.) — skip, don't mark completed
                logger.warning(f"   ⚠️  API error checking '{task_name}' — skipping (won't mark completed)")
                time.sleep(self.config.api_rate_limit_delay)
                continue

            is_completed = task_data.get('completed', False)
            if not is_completed:
                time.sleep(self.config.api_rate_limit_delay)
                continue

            # Update local data to reflect completion
            proj_tasks = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('tasks', {})
            if task_name in proj_tasks:
                proj_tasks[task_name]['status'] = {'name': 'Completed', 'isResolvedStatus': True}
                proj_tasks[task_name]['completed'] = True
                proj_tasks[task_name]['completedTime'] = task_data.get('completedTime')
                completed_count += 1
                logger.info(f"   ✅ Detected completion: '{task_name}'")

            time.sleep(self.config.api_rate_limit_delay)

        if stale_count > 0:
            logger.info(f"   🗑️ Cleaned up {stale_count} stale task(s) no longer in Motion")
        logger.info(f"   📊 Found {completed_count} newly completed task(s) in Motion")
        if (completed_count > 0 or stale_count > 0) and not self.dry_run:
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
        logger.info("🔄 Refreshing Motion tasks from API...")
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
            logger.info(f"   📊 Discovered {new_task_count} new task(s) from Motion API")
            if not self.dry_run:
                self.save_motion_data_to_file(motion_data, update_timestamp=False)
        else:
            logger.info("   📊 No new tasks found in Motion")

        return motion_data

    def sync_motion_to_omnifocus(self) -> Dict:
        """Sync Motion changes back to OmniFocus (reverse sync). Returns reverse stats."""
        logger.info("\n🔄 Starting Motion → OmniFocus synchronization...")

        # Refresh from Motion API to pick up changes and new tasks
        motion_data = self.load_motion_data_from_file()
        motion_data = self.refresh_motion_tasks_from_api(motion_data)
        motion_data = self.refresh_motion_task_statuses(motion_data)

        # Reuse OF structure loaded at start of bidirectional sync
        if not self.of_structure:
            self.of_structure = self.load_omnifocus_structure()
        if not self.of_structure:
            logger.error("❌ Failed to load OmniFocus structure. Cannot proceed with reverse sync.")
            return {'tasks_completed': 0, 'tasks_created': 0, 'failed': 0}

        # Create reverse sync plan (may add auto-mappings to id_mapper)
        reverse_sync_plan = self.create_reverse_sync_plan(motion_data, self.of_structure)

        # Execute reverse sync, passing motion_data to avoid re-loading (which discards auto-mappings)
        reverse_stats = self.execute_reverse_sync_plan(reverse_sync_plan, motion_data)

        logger.info("✅ Reverse synchronization completed!")
        return reverse_stats
    
    def create_reverse_sync_plan(self, motion_data: Dict, of_structure: List[OFFolder]) -> Dict:
        """
        Create a plan for syncing Motion → OmniFocus.
        Handles completions and new task creation.
        """
        logger.info("📋 Creating reverse sync plan (Motion → OmniFocus)...")

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
                                logger.info(f"   📎 Auto-mapped existing: '{task_name}' OF:{existing_of_task.id[:8]}... → Motion:{motion_id[:8]}...")

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

        logger.info(f"   📊 Tasks to complete in OmniFocus: {len(reverse_sync_plan['of_tasks_to_complete'])}")
        logger.info(f"   📊 Tasks to create in OmniFocus: {len(reverse_sync_plan['of_tasks_to_create'])}")
        return reverse_sync_plan
    
    def execute_reverse_sync_plan(self, reverse_sync_plan: Dict, motion_data: Optional[Dict] = None) -> Dict:
        """Execute Motion → OmniFocus sync plan. Returns stats dict."""
        dry = self.dry_run
        if dry:
            logger.info("⚡ Executing reverse sync plan [DRY RUN]...")
        else:
            logger.info("⚡ Executing reverse sync plan...")

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

            if dry:
                completed_count += 1
                logger.info(f"   [DRY RUN] Would complete OF task: '{task_name}'")
            else:
                logger.info(f"   🔄 Completing OF task: '{task_name}'...")
                success = OmniFocusManager.complete_task(of_id)
                if success:
                    completed_count += 1
                    logger.info(f"      ✅ Completed in OmniFocus")
                else:
                    failed_count += 1

        # Create new tasks in OmniFocus
        for task_data in reverse_sync_plan['of_tasks_to_create']:
            task_name = task_data['task_name']
            proj_name = task_data['project']
            motion_id = task_data['motion_id']
            ws_name = task_data['workspace']

            if dry:
                created_count += 1
                logger.info(f"   [DRY RUN] Would create OF task: '{task_name}' in '{proj_name}'")
            else:
                logger.info(f"   ➕ Creating OF task: '{task_name}' in '{proj_name}'...")

                # Build note with Motion link
                ws_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('id', '')
                proj_id = motion_data.get('workspaces', {}).get(ws_name, {}).get('projects', {}).get(proj_name, {}).get('id', '')
                motion_url = f"https://app.usemotion.com/web/pm/workspaces/{ws_id}/projects/{proj_id}/views/default?task={motion_id}"

                desc_body = strip_html(extract_body(task_data.get('description', '') or ''))
                note = build_description(body=desc_body, motion_url=motion_url)

                flagged = task_data.get('priority') == 'ASAP'
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
                    logger.info(f"      ✅ Created in OmniFocus (ID: {new_of_id[:8]}...)")

                    if self.id_mapper:
                        self.id_mapper.add_mapping(
                            of_id=new_of_id,
                            motion_id=motion_id,
                            workspace=ws_name,
                            project=proj_name,
                            task_name=task_name
                        )

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

        if not dry:
            # Always save - auto-mappings from create_reverse_sync_plan need persisting
            has_changes = created_count > 0 or completed_count > 0
            self.save_motion_data_to_file(motion_data, update_timestamp=has_changes)

        logger.info(f"\n📊 Reverse Sync Results:")
        logger.info(f"   ✅ Completed: {completed_count}")
        logger.info(f"   ➕ Created: {created_count}")
        if failed_count > 0:
            logger.error(f"   ❌ Failed: {failed_count}")

        return {'tasks_completed': completed_count, 'tasks_created': created_count, 'failed': failed_count}

    def perform_sync_comparison_from_structure(self, motion_data: Dict) -> Dict:
        """Perform sync comparison using the loaded OmniFocus structure. Returns forward stats."""
        logger.debug("🔍 Performing sync comparison from OF structure...")
        try:
            sync_plan = self.create_sync_plan_from_structure(motion_data)
            return self.execute_sync_plan(sync_plan, motion_data)
        except Exception as e:
            logger.error(f"❌ Error during sync comparison: {e}")
            logger.debug(traceback.format_exc())
            return {'projects_created': 0, 'tasks_created': 0, 'tasks_updated': 0,
                    'tasks_completed': 0, 'failed': 1}

    def create_sync_plan_from_structure(self, motion_data: Dict) -> Dict:
        """Create a sync plan by comparing individual task modification times."""
        logger.info(" Creating sync plan from OF structure...")
        self.last_sync_timestamp = motion_data.get("metadata", {}).get("last_sync_timestamp")
        if self.last_sync_timestamp:
            logger.info(f"ℹ️  Comparing against last sync time: {self.last_sync_timestamp}")

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
                logger.warning(f"⚠️  Warning: No Motion workspace found for OF folder '{folder_name}'")
                logger.info(f"   → Tried to find: '{motion_workspace_name}'")
                logger.info(f"   → Run 'python3 list_motion_workspaces.py' and update workspace_mapping")
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
                            modified_since_sync = (not self.last_sync_timestamp) or (of_mod_time and of_mod_time > self.last_sync_timestamp)

                            # Always check priority (cheap comparison, catches stale mismatches)
                            # Only do full field comparison if OF task was modified after last sync
                            self._check_task_for_updates(task, motion_task, sync_plan, project_name, motion_workspace_name,
                                                         full_check=modified_since_sync)

                    elif not task.completed:
                        task_entry = {
                            'name': task_name, 'project': project_name, 'workspace': folder_name,
                            'of_id': task.id,
                            'due_date': getattr(task, 'due_date', None),
                            'defer_date': getattr(task, 'defer_date', None),
                            'duration_minutes': getattr(task, 'duration_minutes', None),
                            'flagged': getattr(task, 'flagged', False),
                            'of_priority': getattr(task, 'of_priority', None),
                            'note': getattr(task, 'note', None),
                            'url': getattr(task, 'url', None),
                            'contexts': getattr(task, 'contexts', []),
                            'repeat_rule': getattr(task, 'repeat_rule', None),
                            'sequential_project': project.sequential,
                        }
                        sync_plan['tasks_to_create'].append(task_entry)

        # Apply sequential project hints before finalizing the plan
        self._apply_sequential_hints(sync_plan, self.of_structure, motion_data)

        logger.info(f"\n📋 Sync Plan Summary:")
        for key, value in sync_plan.items(): logger.info(f"   {key.replace('_', ' ').title()}: {len(value)}")
        return sync_plan
    
    def _build_seq_metadata(self, position: int, total: int, incomplete_tasks: list) -> str:
        """Build sequence metadata text for below-the-line placement in descriptions."""
        is_first = (position == 0)
        lines = [f"Sequential: Task {position + 1} of {total}"]
        if not is_first:
            blocker = incomplete_tasks[position - 1]
            lines.append(f"Blocked by: {blocker.name}")
        return "\n".join(lines)

    def _strip_seq_hints(self, text: str) -> str:
        """Remove existing sequential hint lines from text (body or metadata)."""
        if not text:
            return ""
        lines = text.split('\n')
        clean = [l for l in lines if not l.startswith('⚡ Sequential:') and not l.startswith('Sequential:') and not l.startswith('Blocked by:')]
        return '\n'.join(clean).strip()

    def _extract_seq_metadata(self, desc: str) -> Optional[str]:
        """Extract existing sequence metadata from a description's metadata section."""
        if not desc or METADATA_SEPARATOR not in desc:
            return None
        metadata_section = desc.split(METADATA_SEPARATOR, 1)[1]
        lines = metadata_section.strip().split('\n')
        seq_lines = [l.strip() for l in lines if l.strip().startswith('Sequential:') or l.strip().startswith('Blocked by:')]
        return "\n".join(seq_lines) if seq_lines else None

    def _apply_sequential_hints(self, sync_plan: Dict, of_structure: List[OFFolder], motion_data: Dict):
        """Apply sequential project metadata to task descriptions (below the separator line).

        For sequential projects:
        - Stores sequence position and blocked-by info as metadata below the ═══ separator
        - Persists sequence metadata to task mappings for reference
        - Injects description updates for existing Motion tasks with missing/outdated metadata
        """
        seq_config = self.config.sequential_project_handling
        if not seq_config.get('enabled', True):
            return

        # Build a map of sequential projects and their incomplete task order
        sequential_projects = {}  # (folder_name, project_name) -> [OFTask, ...]
        for folder in of_structure:
            for project in folder.projects:
                if project.sequential and not project.completed:
                    incomplete_tasks = [t for t in project.tasks if not t.completed]
                    if incomplete_tasks:
                        sequential_projects[(folder.name.strip(), project.name.strip())] = incomplete_tasks

        if not sequential_projects:
            return

        # --- Phase 1: Tag tasks_to_create with sequence metadata ---
        for task_entry in sync_plan.get('tasks_to_create', []):
            project_key = (task_entry.get('workspace', ''), task_entry.get('project', ''))
            if project_key not in sequential_projects:
                continue

            incomplete_tasks = sequential_projects[project_key]
            task_of_id = task_entry.get('of_id')
            if not task_of_id:
                continue

            position = None
            for i, t in enumerate(incomplete_tasks):
                if t.id == task_of_id:
                    position = i
                    break
            if position is None:
                continue

            total = len(incomplete_tasks)

            # Store sequence metadata for mapping persistence
            task_entry['sequence_position'] = position + 1
            if position > 0:
                task_entry['blocked_by'] = [incomplete_tasks[position - 1].id]
            if position < total - 1:
                task_entry['blocks'] = [incomplete_tasks[position + 1].id]

            # Store sequence metadata text — will be placed below separator by build_description
            task_entry['sequence_info'] = self._build_seq_metadata(position, total, incomplete_tasks)

        # --- Phase 2: Ensure existing Motion tasks have correct sequence metadata ---
        reverse_ws_mapping = {v: k for k, v in self.workspace_mapping.items()}
        tasks_being_created = {te.get('of_id') for te in sync_plan.get('tasks_to_create', []) if te.get('of_id')}
        tasks_being_updated = {te.get('motion_task_id') for te in sync_plan.get('tasks_to_update', [])}

        for project_key, incomplete_tasks in sequential_projects.items():
            folder_name, project_name = project_key
            motion_workspace_name = self.workspace_mapping.get(folder_name, folder_name)
            motion_ws_data = motion_data.get('workspaces', {}).get(motion_workspace_name, {})
            motion_proj = motion_ws_data.get('projects', {}).get(project_name, {})
            project_tasks = motion_proj.get('tasks', {})
            if not project_tasks:
                continue

            total = len(incomplete_tasks)
            for position, of_task in enumerate(incomplete_tasks):
                if of_task.id in tasks_being_created:
                    continue  # Will get metadata in Phase 1

                task_name = of_task.name.strip()
                motion_task = project_tasks.get(task_name)
                if not motion_task:
                    continue

                motion_id = motion_task.get('id')
                expected_metadata = self._build_seq_metadata(position, total, incomplete_tasks)

                # Check current description for existing sequence metadata
                current_desc = strip_html(motion_task.get('description', '') or '')
                existing_metadata = self._extract_seq_metadata(current_desc)

                # Also check for old-style body hints (⚡ Sequential:) that need migration
                current_body = extract_body(current_desc)
                has_old_body_hints = '⚡ Sequential:' in current_body or current_body.startswith('Sequential:')

                needs_update = (existing_metadata != expected_metadata) or has_old_body_hints

                if not needs_update:
                    continue

                # Build clean description: strip old hints from body, put metadata below line
                clean_body = self._strip_seq_hints(current_body)
                of_tags = getattr(of_task, 'contexts', []) or []
                of_url = getattr(of_task, 'url', None)
                new_desc = build_description(
                    body=clean_body, tags=of_tags if of_tags else None,
                    of_url=of_url, sequence_info=expected_metadata
                )

                if motion_id in tasks_being_updated:
                    # Enrich existing update entry — merge with any pending body changes
                    for te in sync_plan.get('tasks_to_update', []):
                        if te.get('motion_task_id') == motion_id:
                            existing_desc_update = te.get('updates', {}).get('description')
                            if existing_desc_update:
                                # Pending body change exists — use its body with our sequence metadata
                                updated_body = extract_body(existing_desc_update)
                                merged_clean = self._strip_seq_hints(updated_body)
                                te['updates']['description'] = build_description(
                                    body=merged_clean, tags=of_tags if of_tags else None,
                                    of_url=of_url, sequence_info=expected_metadata
                                )
                            else:
                                te.setdefault('updates', {})['description'] = new_desc
                            break
                else:
                    logger.info(f"   ↪️  Updating sequence metadata for '{task_name}' (description)")
                    sync_plan['tasks_to_update'].append({
                        'motion_task_id': motion_id,
                        'name': task_name,
                        'updates': {'description': new_desc},
                        'workspace': motion_workspace_name,
                        'project': project_name
                    })

    def _check_task_for_updates(self, of_task: OFTask, motion_task: Dict, sync_plan: Dict,
                               project_name: str, workspace_name: str, full_check: bool = True):
        """Check if an existing ACTIVE Motion task needs updates based on OmniFocus data.

        When full_check=False, only priority is compared (cheap, always safe).
        When full_check=True, all fields are compared (due date, duration, description).
        """
        updates_needed, field_changes = {}, []

        # Priority — always checked (cheap comparison, catches stale mismatches)
        of_priority = self.map_of_priority_to_motion(of_task)
        motion_priority = (motion_task.get('priority') or "MEDIUM").upper()
        if of_priority != motion_priority:
            updates_needed['priority'] = of_priority
            field_changes.append("priority")

        if full_check:
            # Due date: normalize both to YYYY-MM-DD
            of_due_date = getattr(of_task, 'due_date', None) or ""
            motion_due = (motion_task.get('dueDate') or "")[:10]
            if of_due_date != motion_due:
                updates_needed['dueDate'] = of_due_date or None
                field_changes.append("due date")

            # Duration: normalize None/0/"NONE" to comparable values
            of_duration = getattr(of_task, 'duration_minutes', None)
            motion_duration = motion_task.get('duration')
            of_dur_normalized = of_duration if of_duration and of_duration != "NONE" else None
            motion_dur_normalized = motion_duration if motion_duration and motion_duration != "NONE" else None
            if of_dur_normalized != motion_dur_normalized:
                updates_needed['duration'] = of_dur_normalized
                field_changes.append("duration")

            # Description: compare body only (strip metadata from both sides)
            # Motion returns HTML in descriptions — strip before comparing
            of_note_body = extract_body(getattr(of_task, 'note', None) or '')
            motion_desc_body = extract_body(strip_html(motion_task.get('description', '') or ''))
            if of_note_body != motion_desc_body:
                of_tags = getattr(of_task, 'contexts', []) or []
                of_url = getattr(of_task, 'url', None)
                updates_needed['description'] = build_description(
                    body=of_note_body, tags=of_tags if of_tags else None, of_url=of_url
                )
                field_changes.append("description")

        if updates_needed:
            task_name = motion_task.get('name', 'Unknown Task')
            logger.info(f"   ↪️  Planning update for '{task_name}' ({', '.join(field_changes)})")
            sync_plan['tasks_to_update'].append({
                'motion_task_id': motion_task['id'], 
                'name': task_name, 
                'updates': updates_needed,
                'workspace': workspace_name,
                'project': project_name
            })

    def execute_sync_plan(self, sync_plan: Dict, motion_data: Dict) -> Dict:
        """Execute the sync plan by creating/updating items in Motion.
        Returns stats dict with counts of operations performed."""
        dry = self.dry_run
        if dry:
            logger.info("⚡ Executing sync plan [DRY RUN]...")
        else:
            logger.info("⚡ Executing sync plan...")
        stats = {'projects_created': 0, 'tasks_created': 0, 'tasks_updated': 0,
                 'tasks_completed': 0, 'failed': 0}
        try:
            # ❌ REMOVED: Motion API doesn't support creating workspaces
            # Workspaces must be created manually in Motion UI
            for workspace_name in sync_plan['workspaces_to_create']:
                logger.warning(f"⚠️  Workspace '{workspace_name}' doesn't exist in Motion.")
                logger.info(f"   → Please create it manually in Motion, or map it to an existing workspace.")
                continue

            for project_data in sync_plan['projects_to_create']:
                project_name = project_data['name']
                of_folder_name = project_data['workspace']
                motion_workspace_name = project_data.get('motion_workspace', of_folder_name)

                if motion_workspace_name in motion_data['workspaces']:
                    if dry:
                        stats['projects_created'] += 1
                        logger.info(f"   [DRY RUN] Would create project '{project_name}' in '{motion_workspace_name}'")
                    else:
                        workspace_id = motion_data['workspaces'][motion_workspace_name]['id']
                        created_proj = MotionSync.create_project_in_workspace(workspace_id, project_name)
                        if created_proj and created_proj.get('id'):
                            motion_data['workspaces'][motion_workspace_name]['projects'][project_name] = {'id': created_proj['id'], 'tasks': {}}
                            stats['projects_created'] += 1
                            logger.info(f"   ✅ Created project '{project_name}' in Motion workspace '{motion_workspace_name}'")
                        else:
                            stats['failed'] += 1
                else:
                    logger.warning(f"   ⚠️  Skipping project '{project_name}' - workspace '{motion_workspace_name}' not found")

            for task_data in sync_plan['tasks_to_create']:
                task_name = task_data['name']
                project_name = task_data['project']
                of_folder_name = task_data['workspace']
                motion_workspace_name = self.workspace_mapping.get(of_folder_name, of_folder_name)

                if motion_workspace_name in motion_data['workspaces'] and project_name in motion_data['workspaces'][motion_workspace_name]['projects']:
                    if dry:
                        stats['tasks_created'] += 1
                        logger.info(f"   [DRY RUN] Would create task '{task_name}' in '{project_name}'")
                    else:
                        project_id = motion_data['workspaces'][motion_workspace_name]['projects'][project_name]['id']
                        of_task_id = task_data.get('of_id')

                        created_task = self._create_task(task_name, project_id, task_data, motion_data, motion_workspace_name)
                        if created_task:
                            stats['tasks_created'] += 1
                            motion_data['workspaces'][motion_workspace_name]['projects'][project_name]['tasks'][task_name] = created_task

                            if of_task_id and created_task.get('id') and self.id_mapper:
                                seq_info = None
                                if task_data.get('sequential_project'):
                                    seq_info = {
                                        'sequential_project': True,
                                        'sequence_position': task_data.get('sequence_position'),
                                        'blocks': task_data.get('blocks', []),
                                        'blocked_by': task_data.get('blocked_by', []),
                                    }
                                self.id_mapper.add_mapping(
                                    of_id=of_task_id,
                                    motion_id=created_task['id'],
                                    workspace=motion_workspace_name,
                                    project=project_name,
                                    task_name=task_name,
                                    sequence_info=seq_info
                                )
                                logger.info(f"      📎 Mapped OF:{of_task_id[:8]}... → Motion:{created_task['id'][:8]}...")

                                workspace_id = motion_data['workspaces'][motion_workspace_name]['id']
                                motion_url = f"https://app.usemotion.com/web/pm/workspaces/{workspace_id}/projects/{project_id}/views/default?task={created_task['id']}"
                                of_note_body = extract_body(task_data.get('note', '') or '')
                                new_note = build_description(body=of_note_body, motion_url=motion_url)
                                OmniFocusManager.update_task(of_task_id, {'note': new_note})
                        else:
                            stats['failed'] += 1

            for task_data in sync_plan['tasks_to_update']:
                if dry:
                    stats['tasks_updated'] += 1
                    logger.info(f"   [DRY RUN] Would update task '{task_data['name']}' ({', '.join(task_data['updates'].keys())})")
                else:
                    updated_task_response = MotionSync.update_task(task_data['motion_task_id'], task_data['updates'])
                    if updated_task_response:
                        stats['tasks_updated'] += 1
                        workspace_name = task_data['workspace']
                        project_name = task_data['project']
                        task_name = task_data['name']
                        if workspace_name in motion_data['workspaces'] and project_name in motion_data['workspaces'][workspace_name]['projects']:
                            motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks'][task_name] = updated_task_response
                    else:
                        stats['failed'] += 1

            for task_data in sync_plan['tasks_to_complete']:
                if dry:
                    stats['tasks_completed'] += 1
                    logger.info(f"   [DRY RUN] Would complete task '{task_data['name']}'")
                else:
                    success = MotionSync.complete_task(task_data['motion_task_id'])
                    if success:
                        stats['tasks_completed'] += 1
                        workspace_name = task_data['workspace']
                        project_name = task_data['project']
                        task_name = task_data['name']
                        if workspace_name in motion_data['workspaces'] and \
                           project_name in motion_data['workspaces'][workspace_name]['projects'] and \
                           task_name in motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks']:

                            motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks'][task_name]['status'] = {'name': 'Completed', 'isResolvedStatus': True}
                            motion_data['workspaces'][workspace_name]['projects'][project_name]['tasks'][task_name]['updatedTime'] = datetime.now(timezone.utc).isoformat()
                    else:
                        stats['failed'] += 1

            if not dry:
                self._backfill_missing_cross_links(motion_data)
                self.save_motion_data_to_file(motion_data, update_timestamp=True)

            logger.info(f"\n📊 Forward Sync Results:")
            logger.info(f"   📁 Projects created: {stats['projects_created']}")
            logger.info(f"   ➕ Tasks created: {stats['tasks_created']}")
            logger.info(f"   ↪️  Tasks updated: {stats['tasks_updated']}")
            logger.info(f"   ✅ Tasks completed: {stats['tasks_completed']}")
            if stats['failed'] > 0:
                logger.error(f"   ❌ Failed: {stats['failed']}")

            return stats
        except Exception as e:
            logger.error(f"❌ Error executing sync plan: {e}")
            logger.debug(traceback.format_exc())
            stats['failed'] += 1
            return stats

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
            defer_date_str=task_data.get('defer_date'),
            task_duration=task_data.get('duration_minutes'),
            schedule_name_to_use=schedule_name,
            priority=motion_priority,
            labels=task_data.get('contexts', []),
            default_due_date_offset=self.config.default_due_date_offset_days,
            sequence_info=task_data.get('sequence_info')
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
        if self.dry_run or not self.id_mapper:
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
            logger.info(f"   🔗 Added Motion URLs to {of_updated} OF task note(s)")

    def backfill_cross_links(self):
        """Backfill cross-reference links between OmniFocus and Motion tasks."""
        motion_data = self.load_motion_data_from_file()

        mappings = self.id_mapper.state_data.get('task_mappings', {}) if self.id_mapper else {}
        if not mappings:
            logger.info("ℹ️  No task mappings found. Skipping cross-link backfill.")
            return

        logger.info(f"\n🔗 Backfilling cross-links for {len(mappings)} mapped tasks...")

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

        logger.info(f"🔗 Cross-link backfill complete: {motion_updated} Motion tasks, {of_updated} OF tasks updated")

    def prune_stale_mappings(self, motion_data: Dict):
        """Remove mappings where both OF and Motion tasks no longer exist.

        Conservative: keeps mappings where only one side is deleted, since
        reverse sync may still need them.
        """
        if not self.id_mapper:
            return

        mappings = self.id_mapper.state_data.get('task_mappings', {})
        if not mappings:
            return

        # Guard: if OF structure wasn't loaded (None), we can't reliably determine
        # which OF tasks still exist — skip to avoid false positives.
        # Note: [] (loaded but empty) is a valid state and should proceed.
        if self.of_structure is None:
            logger.debug("⏭️  Skipping stale mapping cleanup — OmniFocus structure not loaded")
            return

        # Build set of known OF task IDs
        of_task_ids = set()
        if self.of_structure:
            for folder in self.of_structure:
                for project in folder.projects:
                    for task in project.tasks:
                        of_task_ids.add(task.id)

        # Build set of known Motion task IDs from local data
        motion_task_ids = set()
        for ws_data in motion_data.get('workspaces', {}).values():
            for proj_data in ws_data.get('projects', {}).values():
                for task_data in proj_data.get('tasks', {}).values():
                    if task_data.get('id'):
                        motion_task_ids.add(task_data['id'])

        # Find mappings where BOTH sides are gone
        keys_to_remove = []
        for key, mapping in mappings.items():
            of_id = mapping.get('of_id')
            motion_id = mapping.get('motion_id')
            of_gone = of_id and of_id not in of_task_ids
            motion_gone = motion_id and motion_id not in motion_task_ids
            if of_gone and motion_gone:
                keys_to_remove.append(key)

        if not keys_to_remove:
            return

        if self.dry_run:
            logger.info(f"[DRY RUN] Would prune {len(keys_to_remove)} stale mapping(s)")
            return

        for key in keys_to_remove:
            del mappings[key]
        self.id_mapper._rebuild_indexes()
        logger.info(f"🧹 Pruned {len(keys_to_remove)} stale mapping(s) (both OF and Motion tasks deleted)")

        # Persist the pruned state to disk
        self.save_motion_data_to_file(self.id_mapper.state_data, update_timestamp=False)

    def log_workspace_schedule_info(self):
        """Log workspace schedule mapping for reference."""
        logger.debug(" M-> Workspace Schedule Mapping (for reference):")
        for workspace, schedule in self.workspace_schedule_mapping.items():
            logger.info(f"    • {workspace} → {schedule}")
        logger.debug(" M-> Note: Motion automatically assigns workspace schedules to new items")

    def run_workflow(self, refresh_mapping: bool = False, sync_only: bool = False):
        """Main workflow that combines mapping and sync."""
        logger.info("🚀 OmniFocus to Motion Hybrid Sync")
        logger.info("=" * 50)
        
        if refresh_mapping or not self.check_local_data_exists():
            logger.info("📊 Creating/Refreshing Motion data mapping...")
            self.create_comprehensive_mapping()
        
        self.log_workspace_schedule_info()
        
        if not sync_only:
            logger.info("🔄 Starting synchronization...")
            self.sync_omnifocus_to_motion()
        
        logger.info("✅ Workflow completed successfully!")
    
    def run_bidirectional_sync(self, refresh_mapping: bool = False, run_id: str = ""):
        """
        Execute full bidirectional synchronization.
        Loads OmniFocus structure once and shares it across all phases.
        Phase 1: OmniFocus → Motion
        Phase 2: Motion → OmniFocus
        """
        start_time = time.monotonic()
        logger.info("🚀 OmniFocus ↔ Motion Bidirectional Sync")
        logger.info("=" * 50)

        if refresh_mapping or not self.check_local_data_exists():
            logger.info("📊 Creating/Refreshing Motion data mapping...")
            self.create_comprehensive_mapping()

        # Load OmniFocus structure once for the entire sync
        self.of_structure = self.load_omnifocus_structure()
        if not self.of_structure:
            logger.error("❌ Failed to load OmniFocus structure. Cannot proceed.")
            return

        if refresh_mapping and not self.dry_run:
            logger.info("\n" + "=" * 50)
            logger.info("Phase 0: Backfill Cross-Links")
            logger.info("=" * 50)
            self.backfill_cross_links()
        elif refresh_mapping and self.dry_run:
            logger.info("\n[DRY RUN] Would backfill cross-links")

        self.log_workspace_schedule_info()

        logger.info("\n" + "=" * 50)
        logger.info("Phase 1: OmniFocus → Motion")
        logger.info("=" * 50)
        forward_stats = self.sync_omnifocus_to_motion()

        logger.info("\n" + "=" * 50)
        logger.info("Phase 2: Motion → OmniFocus")
        logger.info("=" * 50)
        reverse_stats = self.sync_motion_to_omnifocus()

        # Prune stale mappings (after both sync directions have run)
        # Load state data directly to avoid load_motion_data_from_file() which
        # would create a new id_mapper and discard in-memory mappings
        motion_data = self.state_manager.load()
        self.prune_stale_mappings(motion_data)

        duration = time.monotonic() - start_time

        # Aggregated summary
        logger.info("\n" + "=" * 50)
        logger.info("Sync Summary")
        logger.info("=" * 50)
        logger.info(f"  OF → Motion:  {forward_stats.get('tasks_created', 0)} created, "
                     f"{forward_stats.get('tasks_updated', 0)} updated, "
                     f"{forward_stats.get('tasks_completed', 0)} completed, "
                     f"{forward_stats.get('failed', 0)} failed")
        logger.info(f"  Motion → OF:  {reverse_stats.get('tasks_created', 0)} created, "
                     f"{reverse_stats.get('tasks_completed', 0)} completed, "
                     f"{reverse_stats.get('failed', 0)} failed")
        logger.info(f"  Duration:     {duration:.1f}s")

        # Persist sync history
        append_sync_history(run_id=run_id, duration_seconds=duration,
                            dry_run=self.dry_run,
                            forward_stats=forward_stats, reverse_stats=reverse_stats)

        logger.info("\n✅ Bidirectional sync completed successfully!")

def main():
    """Main function with command line options."""
    parser = argparse.ArgumentParser(description="OmniFocus to Motion Sync v3 Stable")
    parser.add_argument("--refresh-mapping", action="store_true", help="Force refresh of Motion data mapping and then sync")
    parser.add_argument("--sync-only", action="store_true", help="Only run sync, skip mapping creation (uses existing local data)")
    parser.add_argument("--mapping-only", action="store_true", help="Only create/refresh the local mapping file, skip sync")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without making any mutations")
    parser.add_argument("--config", type=str, default="config.json", help="Path to configuration file (default: config.json)")

    args = parser.parse_args()

    # Initialize logging with defaults so Config can log during load
    setup_logging()

    # Load configuration
    config = Config(args.config)

    # Re-initialize logging if config specifies a different directory
    if config.log_directory != LOG_DIR:
        setup_logging(log_dir=config.log_directory)

    # Log sync run correlation ID for traceability
    run_id = uuid.uuid4().hex[:8]
    logger.info(f"🆔 Sync run: {run_id}")

    # Initialize the lock immediately at startup
    lock_handle = acquire_lock(config.lock_file)
    
    api_key = os.getenv("MOTION_API_KEY")
    if not api_key:
        api_key = input("Enter your Motion API key: ").strip()
        if not api_key:
            logger.error("❌ No API key provided. Exiting.")
            return
    
    if args.dry_run:
        logger.info("🔍 DRY RUN MODE — no changes will be made")

    MotionSync.set_api_key(api_key)
    sync = MotionHybridSync(api_key, config, dry_run=args.dry_run)
    
    try:
        if args.mapping_only:
            logger.info("📊 Running mapping creation only...")
            sync.create_comprehensive_mapping()
        elif args.sync_only:
            logger.info("🔄 Running sync only...")
            sync.sync_omnifocus_to_motion()
        else:
            sync.run_bidirectional_sync(refresh_mapping=args.refresh_mapping, run_id=run_id)
            
    except KeyboardInterrupt:
        logger.info("\n⏹️ Process interrupted by user")
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred: {e}")
        logger.debug(traceback.format_exc())

if __name__ == "__main__":
    main()
