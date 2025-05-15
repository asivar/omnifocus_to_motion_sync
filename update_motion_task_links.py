#!/usr/bin/env python3
"""
update_motion_task_links.py — Update existing Motion tasks with OmniFocus URLs.

Core Logic:
1. Retrieve structures (folders, projects, tasks including URL) from OmniFocus.
2. Retrieve structures (workspaces, projects, tasks including ID & description) from Motion.
3. Match workspaces/folders (skipping specified OF folders).
4. Match projects within corresponding workspaces.
5. Match tasks within corresponding projects by name.
6. For matched tasks, check if the Motion description already contains the OF link.
7. If the link is missing, update the Motion task description via API PUT request.
8. Respects Motion API rate limits (12 requests/minute).
9. Provides enhanced logging for 400 Bad Request errors.
10. Provides a report of tasks checked and updated.

Environment Variables:
- MOTION_API_KEY: Required Motion API key.
- OMNIFOCUS_UPDATE_MODE: Optional mode (default: 'dry_run').
    - 'dry_run': Report tasks that would be updated without making changes.
    - 'update': Perform the actual updates on Motion tasks.
- OMNIFOCUS_SKIP_FOLDERS: Comma-separated list of OF folder names to skip (e.g., "Reference,Routines"). Defaults to "Reference,Routines".

Usage:
```bash
# Dry run (default - shows what would be updated)
python3 update_motion_task_links.py

# Perform actual updates
OMNIFOCUS_UPDATE_MODE=update python3 update_motion_task_links.py

# Update, skipping different folders
OMNIFOCUS_SKIP_FOLDERS="Archive" OMNIFOCUS_UPDATE_MODE=update python3 update_motion_task_links.py
```
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque # Used for efficient rate limiting queue
from typing import Dict, List, Set, Optional, Any

import requests # Make sure to install: pip install requests
from dataclasses import dataclass, field

# --- Configuration and API Setup ---
MOTION_API_KEY = os.getenv("MOTION_API_KEY")
# Use a different env var for mode to avoid confusion with the sync script
UPDATE_MODE = os.getenv("OMNIFOCUS_UPDATE_MODE", "dry_run").lower()
# Get folders to skip from env var, default to "Reference,Routines"
DEFAULT_SKIP_FOLDERS = "Reference,Routines"
SKIP_FOLDERS_STR = os.getenv("OMNIFOCUS_SKIP_FOLDERS", DEFAULT_SKIP_FOLDERS)
SKIP_FOLDERS: Set[str] = {name.strip() for name in SKIP_FOLDERS_STR.split(',') if name.strip()}

BASE_URL = "https://api.usemotion.com/v1"
REQUEST_TIMEOUT = 30  # seconds

# Rate Limiting Configuration
RATE_LIMIT_REQUESTS = 12
RATE_LIMIT_WINDOW = 60  # seconds


if not MOTION_API_KEY:
    sys.exit("❌ Error: MOTION_API_KEY environment variable is not set.")

HEADERS = {
    "Accept": "application/json",
    "X-API-Key": MOTION_API_KEY,
    "Content-Type": "application/json", # Important for PUT/POST requests
}

# --- Data Classes (Simplified MotionTask for Update) ---
@dataclass
class MotionTask:
    id: str
    name: str
    projectId: str
    workspaceId: str
    description: Optional[str] = None

@dataclass
class OFTask:
    id: str # OmniFocus Task ID
    name: str
    note: Optional[str] = None # Keep note for potential future use, though not directly used in update link logic
    completed: bool = False
    url: Optional[str] = None # OmniFocus task URL

@dataclass
class OFProject:
    id: str # OmniFocus Project ID
    name: str
    tasks: List[OFTask] = field(default_factory=list)

@dataclass
class OFFolder:
    id: str # OmniFocus Folder ID
    name: str
    projects: List[OFProject] = field(default_factory=list)

# --- Motion API Interaction ---
class MotionAPI:
    """Handles interactions with Motion API, including rate limiting."""
    # Use a deque to store request timestamps for efficient popping from the left
    _request_timestamps: deque = deque()

    @classmethod
    def _enforce_rate_limit(cls):
        """Checks and enforces the API rate limit before making a request."""
        now = time.monotonic()
        # Remove timestamps older than the window
        while cls._request_timestamps and cls._request_timestamps[0] <= now - RATE_LIMIT_WINDOW:
            cls._request_timestamps.popleft()

        # If limit is reached, calculate wait time and sleep
        if len(cls._request_timestamps) >= RATE_LIMIT_REQUESTS:
            oldest_request_in_window = cls._request_timestamps[0]
            wait_time = (oldest_request_in_window + RATE_LIMIT_WINDOW) - now
            if wait_time > 0:
                print(f"⏳ Rate limit ({RATE_LIMIT_REQUESTS}/{RATE_LIMIT_WINDOW}s) reached. Waiting for {wait_time:.2f} seconds...")
                time.sleep(wait_time)
            # Re-check and remove old timestamps after sleeping
            now = time.monotonic()
            while cls._request_timestamps and cls._request_timestamps[0] <= now - RATE_LIMIT_WINDOW:
                 cls._request_timestamps.popleft()

        # Record the timestamp of the upcoming request *before* making it
        cls._request_timestamps.append(now)

    @classmethod
    def _make_request(cls, method: str, endpoint: str, params: Optional[Dict] = None, json_data: Optional[Dict] = None) -> Any:
        """Helper function for making API requests with rate limiting."""
        # Enforce rate limit before making the request
        cls._enforce_rate_limit()

        url = f"{BASE_URL}/{endpoint}"
        request_details = f"({method} {url} | Params: {params} | Body: {json_data})" # Log request details on error

        try:
            response = requests.request(
                method,
                url,
                headers=HEADERS,
                params=params,
                json=json_data,
                timeout=REQUEST_TIMEOUT
            )
            # Check for common errors AFTER the request attempt
            if response.status_code == 401:
                 print(f"❌ Auth Error ({response.status_code}) accessing {url}. Check API Key.")
                 # Let raise_for_status handle it below
            elif response.status_code == 400: # *** DETAILED 400 LOGGING ***
                 print(f"❌ Bad Request Error ({response.status_code}) accessing {url}.")
                 try:
                     # Attempt to parse and print JSON error details from API response
                     error_details = response.json()
                     print(f"   API Error Details: {error_details}")
                 except json.JSONDecodeError:
                     # Fallback if the response body isn't JSON
                     print(f"   API Response Body (non-JSON): {response.text}")
                 # Let raise_for_status handle raising the actual exception
            elif response.status_code == 429:
                 print(f"⏳ Received 429 Rate Limit Exceeded from API despite local limiting accessing {url}. Waiting longer...")
                 time.sleep(RATE_LIMIT_WINDOW) # Wait a full window

            # Raise HTTPError for bad responses (4xx or 5xx).
            # This will include the 400 error after we logged details above.
            response.raise_for_status()

            if response.status_code == 204: return None # No content
            if response.text:
                try: return response.json()
                except json.JSONDecodeError:
                    print(f" M-> Warning: Non-JSON response for {method} {url}. Status: {response.status_code}, Body: {response.text[:100]}...")
                    return None
            return None # Empty body

        except requests.exceptions.RequestException as e:
            # Include request details in the error message
            print(f"🚨 Network/Request Error during {request_details}: {e}")
            # Keep the timestamp added by _enforce_rate_limit to count the failed attempt
            raise

    @classmethod
    def get_workspaces(cls) -> Dict[str, Dict]:
        """Retrieve all workspaces"""
        print(" M-> Fetching Motion workspaces...")
        try:
            # This call uses _make_request, handles rate limit
            data = cls._make_request("GET", "workspaces")
            workspaces = data.get("workspaces", []) if data else []
            print(f" M-> Found {len(workspaces)} workspaces.")
            return {ws.get("name"): ws for ws in workspaces if ws.get("name")}
        except Exception as e:
            print(f"🚨 Failed to get Motion workspaces: {e}")
            return {}

    @classmethod
    def get_projects_in_workspace(cls, workspace_id: str) -> Dict[str, Dict]:
        """Retrieve projects for a specific workspace"""
        # print(f" M-> Fetching projects for workspace ID: {workspace_id}...") # Less verbose
        try:
            # This call uses _make_request, handles rate limit
            data = cls._make_request("GET", "projects", params={"workspaceId": workspace_id})
            projects = data.get("projects", []) if data else []
            # print(f" M-> Found {len(projects)} projects in workspace {workspace_id}.") # Less verbose
            return {p.get("name"): p for p in projects if p.get("name")}
        except Exception as e:
            print(f"🚨 Failed to get projects for workspace {workspace_id}: {e}")
            return {}

    @classmethod
    def get_tasks_in_project(cls, project_id: str) -> Dict[str, MotionTask]:
        """Retrieve tasks for a specific project, handling pagination."""
        # print(f" M-> Fetching tasks for project ID: {project_id}...") # Less verbose
        tasks_dict = {}
        next_cursor = None
        page_count = 0
        max_pages = 50 # Safety break for pagination
        try:
            while page_count < max_pages:
                page_count += 1
                # *** REMOVED 'limit' PARAMETER ***
                params = {"projectId": project_id}
                if next_cursor: params["cursor"] = next_cursor

                # Each paginated request uses _make_request, handles rate limit
                data = cls._make_request("GET", "tasks", params=params)
                if not data: break # Exit if no data returned

                tasks_data = data.get("tasks", [])
                # if not tasks_data and page_count == 1: print(f" M-> No tasks found in project {project_id}.") # Less verbose

                for task_data in tasks_data:
                    task_id = task_data.get("id")
                    task_name = task_data.get("name")
                    task_project_id = task_data.get("projectId")
                    task_workspace_id = task_data.get("workspaceId")

                    if all([task_id, task_name, task_project_id, task_workspace_id]):
                        task = MotionTask(
                            id=task_id, name=task_name,
                            projectId=task_project_id, workspaceId=task_workspace_id,
                            description=task_data.get("description")
                        )
                        # Using name as key - potential issue with duplicate names
                        tasks_dict[task.name] = task
                    # else: print(f" M-> Warning: Skipping task with missing data: {task_data.get('id', 'N/A')}") # Less verbose

                next_cursor = data.get("meta", {}).get("nextCursor")
                if not next_cursor: break # Exit loop if no more pages

            if page_count >= max_pages:
                 print(f" M-> Warning: Reached max page limit ({max_pages}) fetching tasks for project {project_id}. May be incomplete.")

            # print(f" M-> Found {len(tasks_dict)} tasks in project {project_id}.") # Less verbose
            return tasks_dict
        except Exception as e: # Catch exceptions from _make_request (like HTTPError for 400)
            # Log the specific project ID where the error occurred
            print(f"🚨 Failed to get tasks for project {project_id}: {e}")
            # Return empty dict to allow the script to continue with other projects if possible
            return {}


    @classmethod
    def update_task_description(cls, task_id: str, new_description: str) -> bool:
        """Update the description of an existing task."""
        print(f" M-> Attempting to update description for task ID: {task_id}...")
        if UPDATE_MODE == "dry_run":
            print(f" M-> [DRY RUN] Would update task {task_id} description.")
            # Simulate success for dry run flow
            return True
        try:
            # PUT request to update the task.
            # This call uses _make_request, handles rate limit
            payload = {"description": new_description}
            cls._make_request("PUT", f"tasks/{task_id}", json_data=payload)
            # If _make_request didn't raise an exception, assume success for PUT
            # (as it might return None on success depending on API)
            print(f" M-> ✅ Successfully updated description for task {task_id}.")
            return True
        except requests.exceptions.HTTPError as e:
             # Log details already handled by _make_request logging for 400/401/429
             # Just print a general update failure message here
             print(f"🚨 Failed to update task {task_id} due to HTTP Error: {e.response.status_code}")
             return False
        except Exception as e:
            # Handle network or other unexpected errors during the request
            print(f"🚨 Unexpected Error updating task {task_id}: {e}")
            return False


# --- OmniFocus Data Retrieval ---
class OmniFocusData:
    """Handles retrieval of OmniFocus structure using JXA"""
    # Using the same robust get_structure method from the previous script
    @classmethod
    def get_structure(cls) -> List[OFFolder]:
        """Retrieve OmniFocus folder/project/task structure using JXA."""
        print(" OF-> Fetching OmniFocus structure (folders, projects, tasks)...")
        # Same JXA script as before to get tasks with URLs
        jxa_script = r"""
        (() => {
          const of = Application('OmniFocus');
          if (!of.running()) {
              console.error("OmniFocus is not running.");
              return JSON.stringify({error: "OmniFocus is not running."});
          }
          const folders = of.defaultDocument.folders();
          const structure = [];
          folders.forEach(f => {
            if (!f || !f.name || typeof f.name !== 'function' || !f.id || typeof f.id !== 'function') {
                console.warn(`Skipping invalid folder object: ${f}`); return;
            }
            const folderId = f.id(); const folderName = f.name();
            if (!folderId || !folderName) { console.warn(`Skipping folder missing ID/Name: ID=${folderId}, Name=${folderName}`); return; }
            const folderData = { id: folderId, name: folderName, projects: [] };
            try {
                const projects = f.flattenedProjects();
                projects.forEach(p => {
                  if (!p || !p.id || typeof p.id !== 'function' || !p.name || typeof p.name !== 'function') {
                      console.warn(`Skipping invalid project object in folder ${folderName}: ${p}`); return;
                  }
                  const projectId = p.id(); const projectName = p.name();
                  if (!projectId || !projectName) { console.warn(`Skipping project missing ID/Name in folder ${folderName}: ID=${projectId}, Name=${projectName}`); return; }
                  const projectData = { id: projectId, name: projectName, tasks: [] };
                  try {
                      const tasks = p.flattenedTasks();
                      tasks.forEach(t => {
                         if (!t || !t.id || typeof t.id !== 'function' || !t.name || typeof t.name !== 'function') {
                             console.warn(`Skipping invalid task object in project ${projectName}: ${t}`); return;
                         }
                         const taskId = t.id(); const taskName = t.name();
                         if (!taskId || !taskName || taskName === '-----------') return;
                         const taskData = {
                           id: taskId, name: taskName,
                           note: t.note() || null, completed: t.completed(),
                           url: 'omnifocus:///task/' + taskId
                         };
                         projectData.tasks.push(taskData);
                      });
                  } catch (taskError) { console.error(`Error processing tasks for project '${projectName}' (ID: ${projectId}): ${taskError}`); }
                  folderData.projects.push(projectData);
                });
            } catch (projectError) { console.error(`Error processing projects for folder '${folderName}' (ID: ${folderId}): ${projectError}`); }
            structure.push(folderData);
          });
          return JSON.stringify(structure);
        })();
        """
        try:
            # Same execution and parsing logic as before
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", jxa_script],
                text=True, capture_output=True, check=True, encoding='utf-8'
            )
            if '"error":' in result.stdout[:100]:
                 try:
                      error_data = json.loads(result.stdout)
                      print(f"🚨 JXA Error: {error_data.get('error', 'Unknown')}")
                      return []
                 except json.JSONDecodeError:
                      print(f"🚨 JXA Error (raw): {result.stdout}")
                      return []

            raw_data: List[Dict] = json.loads(result.stdout)
            of_structure = []
            # Same conversion loop with validation
            for folder_raw in raw_data:
                if not isinstance(folder_raw, dict) or not folder_raw.get('id') or not folder_raw.get('name'): continue
                folder = OFFolder(id=folder_raw['id'], name=folder_raw['name'])
                for project_raw in folder_raw.get('projects', []):
                     if not isinstance(project_raw, dict) or not project_raw.get('id') or not project_raw.get('name'): continue
                     project = OFProject(id=project_raw['id'], name=project_raw['name'])
                     for task_raw in project_raw.get('tasks', []):
                         if not isinstance(task_raw, dict) or not task_raw.get('id') or not task_raw.get('name'): continue
                         task_url = task_raw.get('url') if isinstance(task_raw.get('url'), str) else None
                         task = OFTask(id=task_raw['id'], name=task_raw['name'], note=task_raw.get('note'),
                                       completed=task_raw.get('completed', False), url=task_url)
                         project.tasks.append(task)
                     folder.projects.append(project)
                of_structure.append(folder)

            print(f" OF-> Found {len(of_structure)} top-level folders processed from OmniFocus.")
            return of_structure
        except subprocess.CalledProcessError as e:
            print(f"🚨 Error executing JXA: {e}")
            if e.stderr: print(f"  Stderr: {e.stderr.strip()}")
            if e.stdout: print(f"  Stdout: {e.stdout.strip()}")
            return []
        except json.JSONDecodeError as e:
            print(f"🚨 Error decoding JXA output: {e}")
            print(f"  Raw Output: {result.stdout[:500]}...")
            return []
        except Exception as e:
            print(f"🚨 Unexpected error getting OF structure: {e}")
            import traceback; traceback.print_exc()
            return []

# --- Update Logic ---
class TaskLinkUpdater:
    """Manages the process of updating Motion task descriptions with OF links."""

    def __init__(self, skip_folders: Set[str]):
        """Initialize updater."""
        self.skip_folders = skip_folders
        self.stats = {
            "folders_processed": 0,
            "folders_skipped": 0,
            "workspaces_matched": 0,
            "projects_processed": 0,
            "projects_matched": 0,
            "tasks_checked": 0,
            "tasks_already_linked": 0,
            "tasks_link_missing": 0,
            "tasks_updated_successfully": 0,
            "tasks_update_failed": 0,
            "motion_tasks_not_found": 0, # OF task exists, but no matching Motion task name
            "get_tasks_api_errors": 0, # Count projects where fetching tasks failed
        }
        self.of_structure: List[OFFolder] = []
        self.motion_workspaces: Dict[str, Dict] = {}

    def _fetch_data(self):
        """Fetch data from both sources."""
        print("\n--- Fetching Data ---")
        self.of_structure = OmniFocusData.get_structure()
        if not isinstance(self.of_structure, list): return False # Error handled in get_structure

        self.motion_workspaces = MotionAPI.get_workspaces()
        if not isinstance(self.motion_workspaces, dict): return False # Error handled in get_workspaces

        print("--- Data Fetch Complete ---")
        return True

    def _find_motion_match(self, of_name: str, motion_dict: Dict[str, Dict]) -> Optional[Dict]:
        """Find a matching item (workspace or project) in Motion dictionary."""
        if of_name is None: return None
        # Try direct match first
        if of_name in motion_dict: return motion_dict[of_name]
        # Try loose match (case-insensitive, stripped) - Only for workspaces/projects
        normalized_of_name = of_name.lower().strip()
        for motion_name, motion_item in motion_dict.items():
            if isinstance(motion_name, str) and motion_name.lower().strip() == normalized_of_name:
                print(f"   -> Found loose match for '{of_name}': '{motion_name}'")
                return motion_item
        return None

    def update_links(self):
        """Main method to find and update missing OF links in Motion tasks."""
        if not self._fetch_data():
            print("❌ Aborting due to data fetch errors.")
            return

        print("\n--- Starting Link Update Process ---")
        print(f"Update Mode: {UPDATE_MODE.upper()}")
        if self.skip_folders:
            print(f"Skipping OmniFocus Folders: {', '.join(sorted(list(self.skip_folders)))}")

        # --- Loop through OF structure ---
        for of_folder in self.of_structure:
            self.stats["folders_processed"] += 1
            folder_name = of_folder.name

            if folder_name in self.skip_folders:
                print(f"\n🚫 Skipping OF Folder: '{folder_name}' (in skip list).")
                self.stats["folders_skipped"] += 1
                continue

            print(f"\n📁 Processing OF Folder: '{folder_name}'")
            # find_motion_match doesn't make API calls itself
            motion_workspace = self._find_motion_match(folder_name, self.motion_workspaces)

            if not motion_workspace:
                print(f"   ❌ No matching Motion workspace found for '{folder_name}'. Skipping folder.")
                continue
            else:
                # print(f"   ✅ Matched Motion Workspace: '{motion_workspace['name']}'") # Less verbose
                self.stats["workspaces_matched"] += 1

            workspace_id = motion_workspace.get('id')
            if not workspace_id:
                 print(f"   ❌ Error: Matched workspace '{motion_workspace.get('name')}' has no ID. Skipping.")
                 continue

            # Get Motion projects for this workspace (uses _make_request)
            motion_projects_in_ws = MotionAPI.get_projects_in_workspace(workspace_id)

            for of_project in of_folder.projects:
                self.stats["projects_processed"] += 1
                project_name = of_project.name
                # print(f"\n  📝 Processing OF Project: '{project_name}'") # Less verbose

                # find_motion_match doesn't make API calls itself
                motion_project = self._find_motion_match(project_name, motion_projects_in_ws)

                if not motion_project:
                     # print(f"     ❌ No matching Motion project found for '{project_name}'. Skipping.") # Less verbose
                     continue
                else:
                    # print(f"     ✅ Matched Motion Project: '{motion_project['name']}'") # Less verbose
                    self.stats["projects_matched"] += 1

                project_id = motion_project.get('id')
                if not project_id:
                    print(f"     ❌ Error: Matched project '{motion_project.get('name')}' has no ID. Skipping.")
                    continue

                # Get Motion tasks for this project (uses _make_request, handles pagination)
                print(f"     Fetching tasks for Motion project '{motion_project['name']}' (ID: {project_id})...")
                # Reset error flag for this project
                get_tasks_error = False
                try:
                    # get_tasks_in_project now returns {} on API error but doesn't raise
                    motion_tasks_in_proj = MotionAPI.get_tasks_in_project(project_id)
                    # Check if the function explicitly returned an empty dict due to an error
                    # (We check this implicitly by seeing if it's empty later)

                except Exception as e:
                     # This catch block might be redundant if get_tasks_in_project handles all exceptions
                     print(f"     🚨 CRITICAL Error during get_tasks_in_project for {project_id}: {e}")
                     motion_tasks_in_proj = {} # Ensure it's an empty dict on unexpected error
                     get_tasks_error = True


                # Check if fetching tasks failed (indicated by the logged error and empty dict)
                # We rely on the logging within get_tasks_in_project for the specific error (like 400)
                if not motion_tasks_in_proj and not of_project.tasks:
                     print(f"     ℹ️ No tasks found in either OF or Motion project '{motion_project['name']}'.")
                     continue # Skip task comparison if both are empty
                elif not motion_tasks_in_proj and of_project.tasks:
                     # Check if the emptiness was due to an API error during fetch
                     # We infer this if OF has tasks but Motion returned none, and rely on previous logs
                     print(f"     ⚠️ Warning: No tasks retrieved from Motion project '{motion_project['name']}'. Possible API error during fetch (check logs above). Skipping task comparison.")
                     self.stats["get_tasks_api_errors"] += 1
                     continue # Skip task comparison for this project


                # --- Compare Tasks and Update ---
                print(f"     Comparing {len(of_project.tasks)} OF tasks with {len(motion_tasks_in_proj)} Motion tasks...")
                for of_task in of_project.tasks:
                    # Skip completed OF tasks as they likely don't need links updated
                    if of_task.completed: continue
                    # Skip OF tasks that don't have a URL generated (shouldn't happen with current JXA)
                    if not of_task.url: continue

                    self.stats["tasks_checked"] += 1
                    task_name = of_task.name

                    # Find corresponding Motion task by name (case-sensitive)
                    motion_task = motion_tasks_in_proj.get(task_name)

                    if not motion_task:
                        # print(f"       - OF Task '{task_name}' not found in Motion project.") # Too verbose
                        self.stats["motion_tasks_not_found"] += 1
                        continue

                    # Check if the OF link already exists in the Motion description
                    motion_desc = motion_task.description if motion_task.description else ""
                    # Define the exact link signature to check for/add
                    link_signature = f"OmniFocus Link: {of_task.url}"

                    if link_signature in motion_desc:
                        # print(f"       ✅ Link already exists for task '{task_name}'.") # Too verbose
                        self.stats["tasks_already_linked"] += 1
                    else:
                        print(f"       🔗 Link missing for task '{task_name}'. Preparing update...")
                        self.stats["tasks_link_missing"] += 1

                        # Construct new description (append link with separator)
                        new_desc = motion_desc
                        # Add separator nicely
                        if new_desc and not new_desc.endswith("\n\n---\n"):
                             if not new_desc.endswith('\n'): new_desc += '\n' # Ensure newline before separator
                             new_desc += "\n---\n"
                        elif not new_desc: # Handle empty description
                             new_desc = "---\n" # Start with separator

                        new_desc += link_signature

                        # Update the task via API (uses _make_request)
                        success = MotionAPI.update_task_description(motion_task.id, new_desc)
                        if success:
                            self.stats["tasks_updated_successfully"] += 1
                            # Optional: Update local cache if needed
                            # motion_task.description = new_desc
                        else:
                            self.stats["tasks_update_failed"] += 1

        # --- Print Summary ---
        print("\n--- Link Update Summary ---")
        print(f"Update Mode: {UPDATE_MODE.upper()}")
        for key, value in self.stats.items():
             if value > 0:
                  title = key.replace('_', ' ').replace(' Of ', ' OF ').title()
                  print(f"  {title}: {value}")
        total_errors = self.stats["tasks_update_failed"] + self.stats["get_tasks_api_errors"]
        if total_errors == 0:
            print("  No errors encountered during task updates or fetching tasks.")
        else:
            print(f"  Total Errors Encountered (Fetch + Update): {total_errors}")


        print("\n✅ Link Update Process Complete!")


# --- Main Execution ---
def main():
    """Main function to run the updater"""
    updater = TaskLinkUpdater(skip_folders=SKIP_FOLDERS)
    updater.update_links()

if __name__ == "__main__":
    print("🚀 Starting Motion Task Link Updater Script (v1.3 - Fixed 400 Error)") # Updated version
    if not MOTION_API_KEY:
        print("❌ Critical Error: MOTION_API_KEY is missing.")
        sys.exit(1)
    print(f"🔑 Using Motion API key ending in: ...{MOTION_API_KEY[-4:]}")
    try:
        import requests
    except ImportError:
        print("❌ Critical Error: 'requests' library not found. Install using: pip install requests")
        sys.exit(1)
    # Add check for collections (standard library, but good practice)
    try:
        from collections import deque
    except ImportError:
         # This should virtually never happen with modern Python
         print("❌ Critical Error: 'collections.deque' not available.")
         sys.exit(1)

    main()

