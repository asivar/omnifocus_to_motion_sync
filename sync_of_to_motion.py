#!/usr/bin/env python3
"""
sync_of_to_motion.py — Synchronize OmniFocus folders/projects/tasks to Motion

Core Synchronization Logic:
1. Retrieve structures (folders, projects with URL & mod_date, tasks with URL, Due Date, Duration, Flagged status, mod_date, completed status) from OmniFocus.
2. Retrieve structures (workspaces, projects, tasks with status, schedule names) from Motion.
3. Match workspaces/folders. Skips OmniFocus folders named "Reference" or "Routines".
4. Synchronize projects between matched workspaces.
   - Adds OmniFocus project URL to the Motion project description.
   - Only processes OF projects new or modified since last successful sync (or if they contain new/modified tasks).
5. Synchronize tasks:
   - If OF task is recently modified & completed, and exists in Motion: DELETE Motion task.
   - Else if OF task is not completed and is new/modified in OF and not in Motion: CREATE Motion task.
     - Adds OF task URL to Motion task description.
     - Sets Due Date and Duration.
     - Sets Motion task priority based on OF flag.
     - Assigns Motion schedule using schedule NAME via autoScheduled.schedule field.
   - Infers projectId and workspaceId for tasks if missing from API response.
6. Optionally create missing workspaces or projects based on SYNC_MODE.
7. Respects Motion API rate limits (12 requests/minute) and handles 429 errors more gracefully.
8. Provide detailed sync report.

Environment Variables:
- MOTION_API_KEY: Required Motion API key.
- OMNIFOCUS_SYNC_MODE: Optional sync mode (default: 'dry_run').
    - 'dry_run': Report differences without making changes.
    - 'sync': Create missing items (OF -> Motion); Delete Motion tasks for completed OF tasks.
    - 'strict': Only sync exact name matches; Task/Project link/schedule/deletion sync follows 'sync' logic.
- OMNIFOCUS_SKIP_FOLDERS: Comma-separated list of OF folder names to skip.

Usage:
```bash
python3 sync_of_to_motion.py
OMNIFOCUS_SYNC_MODE=sync python3 sync_of_to_motion.py
```
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import datetime
from collections import deque
from typing import Dict, List, Set, Optional, Any

import requests
from dataclasses import dataclass, field

# --- Configuration and API Setup ---
MOTION_API_KEY = os.getenv("MOTION_API_KEY")
SYNC_MODE = os.getenv("OMNIFOCUS_SYNC_MODE", "dry_run").lower()
DEFAULT_SKIP_FOLDERS = "Reference,Routines"
SKIP_FOLDERS_STR = os.getenv("OMNIFOCUS_SKIP_FOLDERS", DEFAULT_SKIP_FOLDERS)
SKIP_FOLDERS: Set[str] = {name.strip() for name in SKIP_FOLDERS_STR.split(',') if name.strip()}

WORKSPACE_TO_SCHEDULE_MAP: Dict[str, str] = {
    "OMNIFOCUS FOLDER 1": "Motion Calendar Name 1", "OMNIFOCUS FOLDER 2": "Motion Calendar Name 2",
}

BASE_URL = "https://api.usemotion.com/v1"
REQUEST_TIMEOUT = 30
RATE_LIMIT_REQUESTS = 12
RATE_LIMIT_WINDOW = 60
STATE_FILE = "sync_of_motion_state.json"

if not MOTION_API_KEY:
    sys.exit("❌ Error: MOTION_API_KEY environment variable is not set.")

HEADERS = {
    "Accept": "application/json", "X-API-Key": MOTION_API_KEY,
    "Content-Type": "application/json",
}

# --- Data Classes ---
@dataclass
class MotionTask:
    id: str; name: str; projectId: str; workspaceId: str
    description: Optional[str] = None; dueDate: Optional[str] = None
    duration: Optional[int] = None; schedule: Optional[str] = None
    priority: Optional[str] = None
    # status_id, status_type, status_name are no longer strictly needed if we delete on completion
    # but keeping them doesn't hurt if get_tasks_in_project populates them for other potential uses/logging.

@dataclass
class OFTask:
    id: str; name: str
    note: Optional[str] = None; completed: bool = False
    url: Optional[str] = None; due_date: Optional[str] = None
    duration_minutes: Optional[int] = None
    flagged: bool = False
    of_modification_date: Optional[str] = None

@dataclass
class OFProject:
    id: str; name: str
    url: Optional[str] = None
    of_modification_date: Optional[str] = None
    tasks: List[OFTask] = field(default_factory=list)

@dataclass
class OFFolder:
    id: str; name: str
    projects: List[OFProject] = field(default_factory=list)

@dataclass
class SyncConfig:
    create_missing_workspaces: bool = False; create_missing_projects: bool = False
    create_missing_tasks: bool = False; strict_matching: bool = False
    skip_folders: Set[str] = field(default_factory=set)

# --- Motion API Interaction ---
class MotionSync:
    _request_timestamps: deque = deque()

    @classmethod
    def _enforce_rate_limit(cls):
        now = time.monotonic()
        while cls._request_timestamps and cls._request_timestamps[0] <= now - RATE_LIMIT_WINDOW:
            cls._request_timestamps.popleft()
        if len(cls._request_timestamps) >= RATE_LIMIT_REQUESTS:
            wait_time = (cls._request_timestamps[0] + RATE_LIMIT_WINDOW) - now
            if wait_time > 0:
                print(f"⏳ Rate limit ({RATE_LIMIT_REQUESTS}/{RATE_LIMIT_WINDOW}s) reached. Waiting for {wait_time:.2f} seconds...")
                time.sleep(wait_time)
            now = time.monotonic()
            while cls._request_timestamps and cls._request_timestamps[0] <= now - RATE_LIMIT_WINDOW:
                 cls._request_timestamps.popleft()
        cls._request_timestamps.append(now)

    @classmethod
    def _make_request(cls, method: str, endpoint: str, params: Optional[Dict] = None, json_data: Optional[Dict] = None) -> Any:
        cls._enforce_rate_limit()
        url = f"{BASE_URL}/{endpoint}"
        request_details = f"({method} {url} | Params: {params} | Body: {json_data})"
        response_obj = None
        try:
            response_obj = requests.request(method, url, headers=HEADERS, params=params, json=json_data, timeout=REQUEST_TIMEOUT)
            
            if response_obj.status_code == 429:
                print(f"⏳ Received 429 Rate Limit from API @ {url}. Waiting for {RATE_LIMIT_WINDOW}s and will skip this attempt.")
                time.sleep(RATE_LIMIT_WINDOW)
                return None
            elif response_obj.status_code == 401:
                print(f"❌ Auth Error ({response_obj.status_code}) @ {url}. Check API Key.")
            elif response_obj.status_code == 400:
                print(f"❌ Bad Request ({response_obj.status_code}) @ {url}.")
                try: print(f"   API Error: {response_obj.json()}")
                except json.JSONDecodeError: print(f"   API Response (non-JSON): {response_obj.text}")
            
            response_obj.raise_for_status()
            
            if response_obj.status_code == 204: return None # Typical for successful DELETE
            if response_obj.text:
                try: return response_obj.json()
                except json.JSONDecodeError:
                    print(f" M-> Warn: Non-JSON response {method} {url}. Status: {response_obj.status_code}, Body: {response_obj.text[:200]}...")
                    return None
            return None
        
        except requests.exceptions.HTTPError as e:
            print(f"🚨 HTTP Error during {request_details}: {e}")
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                print(f"   Caught HTTPError 429 for {url} in except block. This should have been handled earlier by explicit check. Returning None.")
                return None
            else:
                raise
        except requests.exceptions.RequestException as e:
            print(f"🚨 Network/Request Error {request_details}: {e}")
            raise

    @classmethod
    def get_workspaces(cls) -> Dict[str, Dict]:
        print(" M-> Fetching Motion workspaces...");
        try:
            data = cls._make_request("GET", "workspaces")
            ws = data.get("workspaces", []) if data else []; print(f" M-> Found {len(ws)} workspaces.")
            return {w.get("name"): w for w in ws if w.get("name")}
        except Exception as e:
            print(f"🚨 Failed to get Motion workspaces: {e}"); return {}

    @classmethod
    def get_projects_in_workspace(cls, workspace_id: str) -> Dict[str, Dict]:
        try:
            data = cls._make_request("GET", "projects", params={"workspaceId": workspace_id})
            proj = data.get("projects", []) if data else []
            return {p.get("name"): p for p in proj if p.get("name")}
        except Exception as e:
            print(f"🚨 Failed to get projects for workspace {workspace_id}: {e}"); return {}


    @classmethod
    def get_schedules(cls) -> Set[str]:
        print(" M-> Fetching Motion schedules..."); found_schedule_names: Set[str] = set()
        try:
            data = cls._make_request("GET", "schedules")
            print(f"   M-> Raw schedule data from API (first 500 chars): {str(data)[:500]}...")
            schedules_list = []
            if isinstance(data, list): schedules_list = data
            elif isinstance(data, dict) and "schedules" in data: schedules_list = data.get("schedules", [])
            elif data is not None: print(f" M-> Warn: Unexpected data format for schedules: {type(data)}.")
            if not schedules_list and data is not None:
                 print(f" M-> Info: No schedules extracted from API response. Raw data was: {str(data)[:500]}...")
            for s_item in schedules_list:
                if isinstance(s_item, dict):
                    name = s_item.get("name")
                    api_id = s_item.get("id")
                    if name:
                        found_schedule_names.add(name)
                        id_info = f"(API ID: {api_id})" if api_id else "(API ID: not provided by API)"
                        print(f"     ✅ Schedule Found: '{name}' {id_info}")
                    else:
                        print(f"     ⚠️ Skipping schedule item due to missing name: {str(s_item)[:200]}")
                else: print(f" M-> Warn: Skipping invalid schedule item (not a dict): {str(s_item)[:200]}")
            print(f" M-> Found {len(found_schedule_names)} unique schedule names from API.")
            if not found_schedule_names and schedules_list:
                 print(" M-> Critical Warn: API returned schedule items, but NONE had a usable name.")
            return found_schedule_names
        except Exception as e: print(f"🚨 Failed to get or process Motion schedules: {e}"); traceback.print_exc(); return set()

    # get_completed_status_id method is no longer needed for delete logic
    # update_task_status method is no longer needed for delete logic

    @classmethod
    def get_tasks_in_project(cls, project_id_context: str, workspace_id_context: str) -> Dict[str, MotionTask]:
        tasks_dict = {}; next_cursor = None; page_count = 0; max_pages = 50
        first_page_api_returned_none = False
        first_page_api_returned_direct_empty_list = False
        first_page_api_returned_nested_empty_list = False
        first_page_api_had_malformed_tasks_key = False
        first_page_api_had_unparsable_tasks = False

        try:
            while page_count < max_pages:
                page_count += 1; params = {"projectId": project_id_context}
                if next_cursor: params["cursor"] = next_cursor
                
                print(f"   M-> API Call: GET /tasks for project {project_id_context}, page {page_count}{' with cursor' if next_cursor else ''}")
                data = cls._make_request("GET", "tasks", params=params)
                
                if data is None:
                    if page_count == 1: first_page_api_returned_none = True
                    break
                
                tasks_data_raw = None
                is_direct_list = False

                if isinstance(data, list):
                    tasks_data_raw = data
                    is_direct_list = True
                    if page_count == 1:
                        print(f"   M-> Info (Project: {project_id_context}, Page 1): API returned a direct list of {len(tasks_data_raw)} item(s).")
                        if not tasks_data_raw: first_page_api_returned_direct_empty_list = True
                elif isinstance(data, dict):
                    tasks_data_raw = data.get("tasks")
                    if page_count == 1:
                        if tasks_data_raw is None:
                            first_page_api_had_malformed_tasks_key = True
                            print(f"   M-> Warn (Project: {project_id_context}, Page 1): API response was a dict but missing 'tasks' key. Response keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
                        elif isinstance(tasks_data_raw, list) and not tasks_data_raw:
                            first_page_api_returned_nested_empty_list = True
                else:
                    if page_count == 1: print(f"   M-> Warn (Project: {project_id_context}, Page 1): API returned unexpected data type: {type(data)}. Response: {str(data)[:200]}...")
                    tasks_data_raw = []

                if not isinstance(tasks_data_raw, list):
                    if page_count == 1 and not first_page_api_had_malformed_tasks_key and not is_direct_list:
                         print(f"   M-> Warn (Project: {project_id_context}, Page 1): 'tasks' key was present but not a list. Type: {type(tasks_data_raw)}. Response: {str(data)[:200]}...")
                    tasks_data_processed = []
                else:
                    tasks_data_processed = tasks_data_raw

                if not tasks_data_processed and page_count > 1: break

                parsed_tasks_on_page = 0
                for td_index, td in enumerate(tasks_data_processed):
                    is_parsable = True
                    missing_keys_reason = []
                    
                    task_item_id = None; task_item_name = None
                    task_item_project_id_from_task = None; task_item_workspace_id_from_task = None
                    final_project_id = None; final_workspace_id = None

                    if not isinstance(td, dict):
                        is_parsable = False; missing_keys_reason.append("item_not_a_dict")
                    else:
                        task_item_id = td.get("id"); task_item_name = td.get("name")
                        task_item_project_id_from_task = td.get("projectId")
                        task_item_workspace_id_from_task = td.get("workspaceId")
                        
                        final_project_id = task_item_project_id_from_task or project_id_context
                        final_workspace_id = task_item_workspace_id_from_task or workspace_id_context
                        
                        if not task_item_id: missing_keys_reason.append(f"id_missing_or_falsy (value: {task_item_id!r})")
                        if not task_item_name: missing_keys_reason.append(f"name_missing_or_falsy (value: {task_item_name!r})")
                        if not final_project_id: missing_keys_reason.append(f"projectId_could_not_be_determined (task_value: {task_item_project_id_from_task!r}, context: {project_id_context!r})")
                        if not final_workspace_id: missing_keys_reason.append(f"workspaceId_could_not_be_determined (task_value: {task_item_workspace_id_from_task!r}, context: {workspace_id_context!r})")
                        
                        if missing_keys_reason: is_parsable = False

                    if is_parsable:
                        # No longer need to parse status_id, status_type, status_name for this feature
                        tasks_dict[task_item_name] = MotionTask(id=task_item_id, name=task_item_name,
                                                                projectId=final_project_id, workspaceId=final_workspace_id,
                                                                description=td.get("description"), dueDate=td.get("dueDate"),
                                                                duration=td.get("duration"), schedule=td.get("schedule"),
                                                                priority=td.get("priority"))
                        parsed_tasks_on_page +=1
                    else:
                        if page_count == 1: first_page_api_had_unparsable_tasks = True
                        print(f"   M-> Warn (Project: {project_id_context}, Page {page_count}, Item {td_index}): Skipping unparsable task item. Reason(s): {', '.join(missing_keys_reason)}. Item data (first 150 chars): {str(td)[:150]}...")

                if page_count == 1 and isinstance(tasks_data_raw, list) and tasks_data_raw and parsed_tasks_on_page == 0:
                    first_page_api_had_unparsable_tasks = True

                next_cursor = data.get("meta", {}).get("nextCursor") if isinstance(data, dict) else None
                if not next_cursor: break
            
            if page_count >= max_pages: print(f" M-> Warn: Max pages ({max_pages}) for tasks in project {project_id_context}.")

            if not tasks_dict:
                if first_page_api_returned_none:
                    print(f"   M-> Final Info (Project: {project_id_context}): API returned no data (None) on first task page. Project might be empty or an API issue occurred (check _make_request logs).")
                elif first_page_api_returned_direct_empty_list:
                    print(f"   M-> Final Info (Project: {project_id_context}): API returned a direct empty list on first page. Motion project is confirmed empty.")
                elif first_page_api_returned_nested_empty_list:
                    print(f"   M-> Final Info (Project: {project_id_context}): API returned an empty task list (tasks: []) on first page. Motion project is confirmed empty or has no matching tasks.")
                elif first_page_api_had_malformed_tasks_key:
                     print(f"   M-> Final Warn (Project: {project_id_context}): API returned data, but 'tasks' key was missing or invalid on first page. Cannot process tasks.")
                elif first_page_api_had_unparsable_tasks:
                     print(f"   M-> Final Warn (Project: {project_id_context}): API returned task items on first page, but none were parsable into valid MotionTask objects (check detailed 'Skipping unparsable' logs above).")
                elif page_count > 1 :
                    print(f"   M-> Final Info (Project: {project_id_context}): Processed {page_count} page(s), but no valid tasks were parsed into tasks_dict.")
                elif page_count == 1 and not is_direct_list and not isinstance(data, dict):
                     print(f"   M-> Final Warn (Project: {project_id_context}): API returned unexpected data type on first page. Cannot process tasks.")
            else:
                print(f"   M-> Final Info (Project: {project_id_context}): Successfully parsed {len(tasks_dict)} tasks after processing {page_count} page(s).")

            return tasks_dict
        except Exception as e:
            print(f"🚨 Exception during get_tasks_in_project for {project_id_context}: {e}")
            print("--- Traceback for get_tasks_in_project ---")
            traceback.print_exc()
            print("--- End Traceback ---")
            return {}

    @classmethod
    def create_workspace(cls, name: str) -> Optional[Dict]:
        print(f" M-> Creating workspace: {name}...")
        if SYNC_MODE == "dry_run": print(f" M-> [DRY RUN] Would create workspace: {name}"); return {"id": f"dry_run_ws_{name}", "name": name}
        try:
            created_ws = cls._make_request("POST", "workspaces", json_data={"name": name})
            if created_ws and created_ws.get('id'): print(f" M-> ✨ Created workspace: {name} (ID: {created_ws['id']})"); return created_ws
            print(f"🚨 Error creating workspace {name}: No ID. Response: {created_ws}"); return None
        except Exception as e: print(f"🚨 Error creating workspace {name}: {e}"); return None

    @classmethod
    def create_project_in_workspace(cls, workspace_id: str, project_name: str, of_project_url: Optional[str] = None) -> Optional[Dict]:
        print(f" M-> Creating project: {project_name} in workspace {workspace_id}...")
        payload = {"name": project_name, "workspaceId": workspace_id}
        description_parts = []
        if of_project_url:
            description_parts.append(f"OmniFocus Project Link: {of_project_url}")
        if description_parts:
            payload["description"] = "\n\n---\n".join(description_parts)
        if SYNC_MODE == "dry_run":
            print(f" M-> [DRY RUN] Would create project: {project_name} with payload {payload}")
            return {"id": f"dry_run_proj_{project_name}", "name": project_name, "workspaceId": workspace_id, "description": payload.get("description")}
        try:
            created_proj = cls._make_request("POST", "projects", json_data=payload)
            if created_proj and created_proj.get('id'):
                print(f" M-> ✨ Created project: {project_name} (ID: {created_proj['id']})")
                return created_proj
            print(f"🚨 Error creating project {project_name}: No ID. Response: {created_proj}. Payload: {json.dumps(payload)}")
            return None
        except Exception as e:
            print(f"🚨 Error creating project {project_name}: {e}. Payload: {json.dumps(payload)}")
            return None

    @classmethod
    def create_task_in_project(cls, project_id: str, workspace_id: str, task_name: str,
                               description: Optional[str] = None, of_task_url: Optional[str] = None,
                               due_date_str: Optional[str] = None, duration_minutes: Optional[int] = None,
                               schedule_name_to_use: Optional[str] = None,
                               priority: str = "MEDIUM") -> Optional[Dict]:
        print(f" M-> Creating task: {task_name} in project {project_id}...")
        payload = {
            "name": task_name,
            "projectId": project_id,
            "workspaceId": workspace_id,
            "priority": priority
        }
        
        final_description_parts = []
        if description: final_description_parts.append(description)
        if of_task_url: final_description_parts.append(f"OmniFocus Link: {of_task_url}")
        if final_description_parts: payload["description"] = "\n\n---\n".join(final_description_parts)
        
        current_due_date = due_date_str

        if schedule_name_to_use:
            auto_scheduled_payload = {"schedule": schedule_name_to_use}
            print(f" M-> Assigning Schedule by NAME: '{schedule_name_to_use}' to task '{task_name}' via autoScheduled.schedule")
            if not current_due_date:
                default_due_datetime = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=14)
                current_due_date = default_due_datetime.strftime("%Y-%m-%d")
                print(f"   M-> No OF due date, default dueDate for scheduled task: {current_due_date}")
            payload["autoScheduled"] = auto_scheduled_payload
        else:
            print(f" M-> Task '{task_name}' will use Motion's default scheduling (no specific schedule mapped).")

        if current_due_date:
            payload["dueDate"] = current_due_date
        
        if duration_minutes is not None and duration_minutes > 0:
            payload["duration"] = duration_minutes
        
        print(f" M-> Setting task priority to: {priority}")

        if SYNC_MODE != "sync":
            print(f" M-> [{SYNC_MODE.upper()}] Would create task: {task_name} with payload {json.dumps(payload, indent=2)}")
            return {"id": f"dry_run_task_{task_name}", "name": task_name, **payload}
        try:
            created_task = cls._make_request("POST", "tasks", json_data=payload)
            if created_task and created_task.get('id'): print(f" M-> ✨ Created task: {task_name} (ID: {created_task['id']})"); return created_task
            print(f"🚨 Error creating task '{task_name}': No ID. Response: {created_task}. Payload: {json.dumps(payload)}"); return None
        except Exception as e: print(f"🚨 Error creating task '{task_name}': {e}. Payload: {json.dumps(payload)}"); return None

    # *** ADDED METHOD ***
    @classmethod
    def delete_task(cls, motion_task_id: str) -> bool:
        """Deletes a task in Motion."""
        print(f" M-> Attempting to DELETE Motion task ID: {motion_task_id}...")
        if SYNC_MODE == "dry_run":
            print(f" M-> [DRY RUN] Would DELETE Motion task {motion_task_id}.")
            return True
        try:
            cls._make_request("DELETE", f"tasks/{motion_task_id}")
            # DELETE often returns 204 No Content, which _make_request handles by returning None
            print(f" M-> ✅ Successfully DELETED Motion task {motion_task_id}.")
            return True
        except requests.exceptions.HTTPError as e:
             if hasattr(e, 'response') and e.response is not None:
                  print(f"🚨 HTTP Error DELETING task {motion_task_id}: {e.response.status_code}")
                  try: print(f"   API Error Details: {e.response.json()}")
                  except json.JSONDecodeError: print(f"   API Response Body: {e.response.text}")
             else: print(f"🚨 HTTP Error DELETING task {motion_task_id}: {e}")
             return False
        except Exception as e:
            print(f"🚨 Unexpected Error DELETING task {motion_task_id}: {e}")
            return False

# --- OmniFocus Data Retrieval ---
class OmniFocusSync:
    @classmethod
    def get_structure(cls) -> List[OFFolder]:
        print(" OF-> Fetching OmniFocus structure...")
        jxa_script = r"""
        (() => {
          const of = Application('OmniFocus');
          if (!of.running()) { return JSON.stringify({error: "OmniFocus is not running."}); }
          const formatDate = (date) => {
            if (!date) return null;
            return `${date.getFullYear()}-${(date.getMonth() + 1).toString().padStart(2, '0')}-${date.getDate().toString().padStart(2, '0')}`;
          };
          const folders = of.defaultDocument.folders();
          const structure = [];
          folders.forEach(f => {
            if (!f || !f.name || typeof f.name !== 'function' || !f.id || typeof f.id !== 'function') return;
            const folderId = f.id(); const folderName = f.name();
            if (!folderId || !folderName) return;
            const folderData = { id: folderId, name: folderName, projects: [] };
            try {
                f.flattenedProjects().forEach(p => {
                  if (!p || !p.id || typeof p.id !== 'function' || !p.name || typeof p.name !== 'function') return;
                  const projectId = p.id(); const projectName = p.name();
                  if (!projectId || !projectName) return;
                  const projectUrl = 'omnifocus:///project/' + projectId;
                  const projectModDate = p.modificationDate() ? p.modificationDate().toISOString() : null; 
                  const projectData = { id: projectId, name: projectName, url: projectUrl, modificationDate: projectModDate, tasks: [] };
                  try {
                      p.flattenedTasks().forEach(t => {
                         if (!t || !t.id || typeof t.id !== 'function' || !t.name || typeof t.name !== 'function') return;
                         const taskId = t.id(); const taskName = t.name();
                         if (!taskId || !taskName || taskName === '-----------') return;
                         let dueDateFormatted = null; try { const dr = t.dueDate(); if (dr) dueDateFormatted = formatDate(dr); } catch (e) {}
                         let estimatedMins = null; try { const mr = t.estimatedMinutes(); if (typeof mr === 'number' && mr > 0) estimatedMins = mr; } catch (e) {}
                         let isFlagged = false; try { isFlagged = t.flagged(); } catch (e) {} 
                         let taskModDate = t.modificationDate() ? t.modificationDate().toISOString() : null; 
                         let isCompleted = false; try { isCompleted = t.completed(); } catch (e) {}
                         projectData.tasks.push({ 
                             id: taskId, name: taskName, 
                             note: t.note() || null, completed: isCompleted,
                             url: 'omnifocus:///task/' + taskId, 
                             dueDate: dueDateFormatted, 
                             durationMinutes: estimatedMins,
                             flagged: isFlagged,
                             modificationDate: taskModDate 
                         });
                      });
                  } catch (taskError) { console.error(`Error tasks for project '${projectName}': ${taskError}`); }
                  folderData.projects.push(projectData);
                });
            } catch (projectError) { console.error(`Error projects for folder '${folderName}': ${projectError}`); }
            structure.push(folderData);
          });
          return JSON.stringify(structure);
        })();
        """
        try:
            result = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa_script], text=True, capture_output=True, check=True, encoding='utf-8')
            if '"error":' in result.stdout[:100]:
                 try: error_data = json.loads(result.stdout); print(f"🚨 JXA Error: {error_data.get('error', 'Unknown')}"); return []
                 except json.JSONDecodeError: print(f"🚨 JXA Error (raw): {result.stdout}"); return []
            raw_data: List[Dict] = json.loads(result.stdout)
            of_structure = []
            for fr in raw_data:
                if not isinstance(fr, dict) or not fr.get('id') or not fr.get('name'): continue
                folder = OFFolder(id=fr['id'], name=fr['name'])
                for pr in fr.get('projects', []):
                     if not isinstance(pr, dict) or not pr.get('id') or not pr.get('name'): continue
                     proj_url = pr.get('url') if isinstance(pr.get('url'), str) else None
                     proj_mod_date = pr.get('modificationDate')
                     project = OFProject(id=pr['id'], name=pr['name'], url=proj_url, of_modification_date=proj_mod_date)
                     for tr in pr.get('tasks', []):
                         if not isinstance(tr, dict) or not tr.get('id') or not tr.get('name'): continue
                         task_mod_date = tr.get('modificationDate')
                         task = OFTask(id=tr['id'], name=tr['name'], note=tr.get('note'),
                                       completed=tr.get('completed', False), url=tr.get('url'),
                                       due_date=tr.get('dueDate'), duration_minutes=tr.get('durationMinutes'),
                                       flagged=tr.get('flagged', False), of_modification_date=task_mod_date)
                         project.tasks.append(task)
                     folder.projects.append(project)
                of_structure.append(folder)
            print(f" OF-> Found {len(of_structure)} OF folders processed."); return of_structure
        except subprocess.CalledProcessError as e: print(f"🚨 Error JXA: {e.stderr or e.stdout or e}"); return []
        except json.JSONDecodeError as e: print(f"🚨 Error JXA JSON: {e}. Output: {result.stdout[:500]}..."); return []
        except Exception as e: print(f"🚨 Unexpected OF error: {e}"); traceback.print_exc(); return []

# --- Synchronization Logic ---
class ProjectSynchronizer:
    def __init__(self, config: SyncConfig):
        self.config = config
        self.stats = { "folders_processed": 0, "folders_skipped": 0, "workspaces_matched": 0, "workspaces_created": 0,
                       "projects_processed": 0, "projects_skipped_not_modified": 0,
                       "projects_matched": 0, "projects_created": 0, "projects_link_added": 0,
                       "tasks_processed": 0, "tasks_skipped_not_modified": 0,
                       "tasks_skipped_due_to_unmodified_project": 0,
                       "tasks_matched": 0, "tasks_created": 0,
                       "tasks_deleted_in_motion": 0, # New Stat
                       "tasks_created_with_due_date": 0, "tasks_created_with_duration": 0,
                       "tasks_created_with_schedule": 0,
                       "tasks_created_with_high_priority": 0,
                       "tasks_created_with_medium_priority": 0,
                       "tasks_skipped_completed_of": 0,
                       "errors_workspace_creation": 0, "errors_project_creation": 0, "errors_task_creation": 0,
                       "errors_task_deletion": 0, # New Stat
                       "get_tasks_api_errors": 0, "schedule_mapping_warnings": 0,
                     }
        self.of_structure: List[OFFolder] = []
        self.motion_workspaces: Dict[str, Dict] = {}
        self.motion_schedule_names: Set[str] = set()
        self.last_sync_timestamp: Optional[str] = self._load_last_sync_timestamp()
        # Removed motion_completed_status_id and workspace_completed_status_ids
        print(f"ℹ️ Loaded last sync timestamp: {self.last_sync_timestamp or 'None (first run or state file missing)'}")


    def _load_last_sync_timestamp(self) -> Optional[str]:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    state_data = json.load(f)
                    return state_data.get("last_run_utc_iso_timestamp")
        except (IOError, json.JSONDecodeError) as e:
            print(f"⚠️ Error loading state file '{STATE_FILE}': {e}. Will perform a full sync.")
        return None

    def _save_last_sync_timestamp(self, timestamp_str: str):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({"last_run_utc_iso_timestamp": timestamp_str}, f, indent=2)
            print(f"💾 Saved current sync timestamp: {timestamp_str} to '{STATE_FILE}'")
        except IOError as e:
            print(f"⚠️ Error saving state file '{STATE_FILE}': {e}")


    def _fetch_data(self):
        print("\n--- Fetching Data ---")
        self.of_structure = OmniFocusSync.get_structure()
        if not isinstance(self.of_structure, list): print("❌ Critical: Failed OF structure fetch."); return False
        elif not self.of_structure: print("ℹ️ Info: No OF folders found.")
        
        self.motion_workspaces = MotionSync.get_workspaces()
        if not isinstance(self.motion_workspaces, dict): print("❌ Critical: Failed Motion workspaces fetch."); return False
        elif not self.motion_workspaces: print("ℹ️ Info: No Motion workspaces found.")
        
        self.motion_schedule_names = MotionSync.get_schedules()
        if not isinstance(self.motion_schedule_names, set): print("⚠️ Warn: Failed Motion schedules fetch or no usable schedule names found.")
            
        print("--- Data Fetch Complete ---"); return True

    def _find_motion_match(self, of_name: str, motion_dict: Dict[str, Dict]) -> Optional[Dict]:
        if of_name is None: return None
        if of_name in motion_dict: return motion_dict[of_name]
        if not self.config.strict_matching:
            norm_of_name = of_name.lower().strip()
            for m_name, m_item in motion_dict.items():
                if isinstance(m_name, str) and m_name.lower().strip() == norm_of_name:
                    print(f"   -> Loose match for '{of_name}': '{m_name}'"); return m_item
        return None

    def sync(self):
        if not self._fetch_data(): return
        print("\n--- Starting Synchronization ---"); print(f"Sync Mode: {SYNC_MODE.upper()}")
        print(f"Config: {self.config}"); print(f"Skip Folders: {self.config.skip_folders or 'None'}")
        print(f"Workspace->Schedule Map: {WORKSPACE_TO_SCHEDULE_MAP or 'None'}")
        print(f"Verified Motion Schedule Names: {self.motion_schedule_names or 'None found'}")

        overall_sync_successful = True

        try:
            for of_folder in self.of_structure:
                self.stats["folders_processed"] += 1; folder_name = of_folder.name
                if folder_name in self.config.skip_folders:
                    print(f"\n🚫 Skipping OF Folder: '{folder_name}'"); self.stats["folders_skipped"] += 1; continue
                print(f"\n📁 Processing OF Folder: '{folder_name}'")
                motion_ws = self._find_motion_match(folder_name, self.motion_workspaces)
                if not motion_ws:
                    if self.config.create_missing_workspaces:
                        print(f"   Creating Motion workspace for '{folder_name}'...")
                        created_ws = MotionSync.create_workspace(folder_name)
                        if created_ws: motion_ws = created_ws; self.stats["workspaces_created"] += 1
                        if SYNC_MODE != "dry_run" and created_ws: self.motion_workspaces[created_ws['name']] = created_ws
                        else: print(f"   ❌ Fail create workspace '{folder_name}'. Skip."); self.stats["errors_workspace_creation"] += 1; overall_sync_successful=False; continue
                    else: print(f"   ❌ No Motion workspace for '{folder_name}'. Skip."); continue
                else: print(f"   ✅ Matched Motion Workspace: '{motion_ws['name']}' (ID: {motion_ws.get('id')})"); self.stats["workspaces_matched"] += 1
                
                ws_id = motion_ws.get('id')
                if not ws_id: print(f"   ❌ Workspace '{motion_ws.get('name')}' no ID. Skip."); self.stats["errors_workspace_creation"]+=1; overall_sync_successful=False; continue
                
                motion_projs_in_ws = {}
                if not ws_id.startswith("dry_run_ws_"): motion_projs_in_ws = MotionSync.get_projects_in_workspace(ws_id)

                for of_project in of_folder.projects:
                    self.stats["projects_processed"] += 1; proj_name = of_project.name
                    
                    project_is_new_or_modified_in_of = (not self.last_sync_timestamp) or \
                                                       (of_project.of_modification_date and \
                                                        self.last_sync_timestamp and \
                                                        of_project.of_modification_date > self.last_sync_timestamp)
                    
                    if not project_is_new_or_modified_in_of and self.last_sync_timestamp:
                        any_task_modified_in_project = False
                        for of_task_check in of_project.tasks:
                            if (not self.last_sync_timestamp or
                                (of_task_check.of_modification_date and \
                                 of_task_check.of_modification_date > self.last_sync_timestamp)):
                                any_task_modified_in_project = True
                                break
                        if not any_task_modified_in_project:
                            print(f"  ⏭️ Skipping OF Project '{proj_name}' (project & its tasks not modified in OF since {self.last_sync_timestamp})")
                            self.stats["projects_skipped_not_modified"] += 1
                            self.stats["tasks_skipped_due_to_unmodified_project"] = self.stats.get("tasks_skipped_due_to_unmodified_project", 0) + len(of_project.tasks)
                            continue
                        else:
                            print(f"  ℹ️ OF Project '{proj_name}' itself not modified, but contains modified tasks. Processing project.")
                            project_is_new_or_modified_in_of = True
                    
                    print(f"\n  📝 Processing OF Project: '{proj_name}'" + (" (new/modified in OF)" if project_is_new_or_modified_in_of and self.last_sync_timestamp else ""))

                    motion_proj_initially_exists = self._find_motion_match(proj_name, motion_projs_in_ws)
                    motion_proj = motion_proj_initially_exists
                    motion_project_was_created_this_run = False

                    if not motion_proj:
                        if self.config.create_missing_projects:
                            print(f"     Creating Motion project for '{proj_name}'...")
                            created_proj = MotionSync.create_project_in_workspace(ws_id, proj_name, of_project_url=of_project.url)
                            if created_proj:
                                motion_proj = created_proj; self.stats["projects_created"] += 1
                                motion_project_was_created_this_run = True
                                if of_project.url: self.stats["projects_link_added"] += 1
                                if SYNC_MODE != "dry_run": motion_projs_in_ws[created_proj['name']] = created_proj
                            else: print(f"     ❌ Fail create project '{proj_name}'. Skip."); self.stats["errors_project_creation"] += 1; overall_sync_successful=False; continue
                        else: print(f"     ❌ No Motion project for '{proj_name}'. Skip."); continue
                    else: print(f"     ✅ Matched Motion Project: '{motion_proj['name']}' (ID: {motion_proj.get('id')})"); self.stats["projects_matched"] += 1
                    
                    proj_id = motion_proj.get('id')
                    if not proj_id: print(f"     ❌ Project '{motion_proj.get('name')}' no ID. Skip."); self.stats["errors_project_creation"]+=1; overall_sync_successful=False; continue

                    # Fetch tasks from Motion for comparison and potential deletion
                    motion_tasks_in_proj_for_this_project = {} # Use a local variable for this project's tasks
                    if not proj_id.startswith("dry_run_proj_"):
                        print(f"       Fetching tasks for Motion project '{motion_proj['name']}' (ID: {proj_id})...")
                        motion_tasks_in_proj_for_this_project = MotionSync.get_tasks_in_project(proj_id, ws_id)
                        # Note: get_tasks_api_errors is incremented within get_tasks_in_project if it has issues,
                        # but it returns {} on error, so we still proceed.
                    
                    print(f"       Comparing {len(of_project.tasks)} OF tasks with {len(motion_tasks_in_proj_for_this_project)} Motion tasks for project '{proj_name}'...")
                    for of_task in of_project.tasks:
                        self.stats["tasks_processed"] += 1; task_name = of_task.name
                        
                        task_is_new_or_modified_in_of = (not self.last_sync_timestamp) or \
                                                        (of_task.of_modification_date and \
                                                         self.last_sync_timestamp and \
                                                         of_task.of_modification_date > self.last_sync_timestamp)
                        
                        motion_task_data = motion_tasks_in_proj_for_this_project.get(task_name)

                        if of_task.completed and task_is_new_or_modified_in_of and motion_task_data:
                            print(f"         🔄 OF task '{task_name}' is completed and recent. Attempting to DELETE Motion task ID: {motion_task_data.id}.")
                            if MotionSync.delete_task(motion_task_data.id):
                                self.stats["tasks_deleted_in_motion"] += 1
                                # Remove from our local cache so we don't try to act on it again
                                if task_name in motion_tasks_in_proj_for_this_project:
                                    del motion_tasks_in_proj_for_this_project[task_name]
                            else:
                                self.stats["errors_task_deletion"] += 1; overall_sync_successful=False
                            self.stats["tasks_skipped_completed_of"] += 1 # Still count as processed completed OF task
                            continue # Move to next OF task

                        if of_task.completed:
                            self.stats["tasks_skipped_completed_of"] += 1
                            continue

                        # Task is NOT completed in OF. Process for creation if new/modified.
                        if not self.last_sync_timestamp or motion_project_was_created_this_run or task_is_new_or_modified_in_of:
                            if motion_task_data: # Task with same name exists in Motion
                                self.stats["tasks_matched"] += 1
                                # print(f"         Task '{task_name}' already exists in Motion and OF task not marked for completion-deletion. Skipping creation.")
                                continue
                            
                            # Task does not exist in Motion, proceed to create
                            if self.config.create_missing_tasks:
                                print(f"         ✨ Creating Motion task for '{task_name}'" +
                                      (" (new/modified in OF)" if task_is_new_or_modified_in_of and not motion_project_was_created_this_run else "") +
                                      (" (Motion project new)" if motion_project_was_created_this_run else "")
                                )
                                
                                schedule_name_to_pass = None
                                target_schedule_name_from_map = WORKSPACE_TO_SCHEDULE_MAP.get(folder_name)
                                if target_schedule_name_from_map:
                                    if target_schedule_name_from_map in self.motion_schedule_names:
                                        schedule_name_to_pass = target_schedule_name_from_map
                                    else:
                                        print(f"         ⚠️ Warn: Schedule name '{target_schedule_name_from_map}' (mapped for workspace '{folder_name}') not found in verified Motion schedule names. Task will use Motion default scheduling.")
                                        self.stats["schedule_mapping_warnings"]+=1
                                
                                task_priority = "HIGH" if of_task.flagged else "MEDIUM"
                                
                                created_task = MotionSync.create_task_in_project(
                                    proj_id, ws_id, task_name, of_task.note, of_task.url,
                                    of_task.due_date, of_task.duration_minutes,
                                    schedule_name_to_pass, priority=task_priority)
                                if created_task:
                                    self.stats["tasks_created"] += 1
                                    if of_task.due_date: self.stats["tasks_created_with_due_date"] += 1
                                    if of_task.duration_minutes: self.stats["tasks_created_with_duration"] += 1
                                    if schedule_name_to_pass: self.stats["tasks_created_with_schedule"] += 1
                                    if task_priority == "HIGH": self.stats["tasks_created_with_high_priority"] +=1
                                    else: self.stats["tasks_created_with_medium_priority"] +=1

                                    if SYNC_MODE != "dry_run" and created_task.get('id'):
                                        # Add newly created task to local cache
                                        motion_tasks_in_proj_for_this_project[created_task['name']] = MotionTask(
                                            id=created_task['id'], name=created_task['name'], projectId=proj_id, workspaceId=ws_id,
                                            description=created_task.get('description'), dueDate=created_task.get('dueDate'),
                                            duration=created_task.get('duration'), schedule=created_task.get("schedule"),
                                            priority=created_task.get("priority")
                                            # Assuming new tasks don't have status info immediately available from create response in same detail
                                        )
                                else: print(f"         ❌ Fail create task '{task_name}'."); self.stats["errors_task_creation"] += 1; overall_sync_successful=False
                            else:
                                target_schedule_name_for_report = WORKSPACE_TO_SCHEDULE_MAP.get(folder_name)
                                sched_name_for_report = "Motion Default"
                                if target_schedule_name_for_report:
                                    sched_name_for_report = target_schedule_name_for_report
                                    if target_schedule_name_for_report not in self.motion_schedule_names:
                                        sched_name_for_report += " (NOT VERIFIED in Motion)"
                                sched_info_dr = f" (Schedule: {sched_name_for_report})"
                                priority_info_dr = " (Priority: HIGH)" if of_task.flagged else " (Priority: MEDIUM)"
                                due_info = f" (Due: {of_task.due_date})" if of_task.due_date else ""
                                dur_info = f" (Dur: {of_task.duration_minutes}m)" if of_task.duration_minutes else ""
                                
                                print(f"         [{SYNC_MODE.upper()}] Would create task '{task_name}'{due_info}{dur_info}{sched_info_dr}{priority_info_dr}.")
                        else:
                            print(f"         ⏭️ Skipping OF Task '{task_name}' (not modified since last sync: {self.last_sync_timestamp} and Motion project not new).")
                            self.stats["tasks_skipped_not_modified"] += 1
                            continue
        except Exception as e:
            print(f"🚨 UNEXPECTED ERROR during synchronization: {e}")
            traceback.print_exc()
            overall_sync_successful = False
        finally:
            print("\n--- Synchronization Summary ---"); print(f"Sync Mode: {SYNC_MODE.upper()}")
            for k, v in self.stats.items():
                if v > 0: print(f"  {k.replace('_', ' ').replace(' Of ', ' OF ').title()}: {v}")
            total_err = sum(self.stats[e] for e in ["errors_workspace_creation", "errors_project_creation", "errors_task_creation", "errors_task_deletion", "get_tasks_api_errors"])
            if total_err == 0 and self.stats["schedule_mapping_warnings"] == 0: print("  No errors or warnings.")
            else:
                if total_err > 0: print(f"  Total Errors Encountered: {total_err}")
                if self.stats["schedule_mapping_warnings"] > 0: print(f"  Schedule Mapping Warnings: {self.stats['schedule_mapping_warnings']}")
            
            if overall_sync_successful and SYNC_MODE == 'sync':
                current_utc_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds') + "Z"
                self._save_last_sync_timestamp(current_utc_timestamp)
            elif SYNC_MODE == 'sync':
                print("⚠️ Sync was not fully successful or an error occurred. Last sync timestamp NOT updated.")

            print("\n✅ Sync Process Complete!")


def main():
    config = SyncConfig(create_missing_workspaces=(SYNC_MODE == 'sync'), create_missing_projects=(SYNC_MODE == 'sync'),
                        create_missing_tasks=(SYNC_MODE == 'sync'), strict_matching=(SYNC_MODE == 'strict'), skip_folders=SKIP_FOLDERS)
    ProjectSynchronizer(config).sync()

if __name__ == "__main__":
    print("🚀 Starting OmniFocus -> Motion Sync Script (v1.4 - Delete Motion Task on OF Completion)")
    if not MOTION_API_KEY: sys.exit("❌ Critical: MOTION_API_KEY missing.")
    print(f"🔑 Using Motion API key ending: ...{MOTION_API_KEY[-4:]}")
    try: import requests; from collections import deque; import traceback; import datetime
    except ImportError as e: sys.exit(f"❌ Critical: Missing import: {e}")
    main()

