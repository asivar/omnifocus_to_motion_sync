#!/usr/bin/env python3
"""
compare_projects.py — Cross‑check OmniFocus folders ⇄ Motion workspaces ‑and‑
projects within each workspace.

Logic
-----
1. **OmniFocus**
   * Each *top‑level folder* ⇒ treated as a Motion *workspace*.
   * For every folder we gather the *flattened projects* it contains.
2. **Motion**
   * Fetch **all** workspaces visible to the API key.
   * For each workspace, list every project in that workspace.
3. **Comparison**
   * Report folders missing in Motion, workspaces missing in OmniFocus, and the
     intersections.
   * For each matched folder⇄workspace pair, list project‑level diffs.

Usage
-----
```
# activate your venv if you use one
env MOTION_API_KEY=sk-live-… python3 compare_projects.py
```
Only `MOTION_API_KEY` is required now; we auto‑discover all workspaces.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Dict, List, Set

import requests

API_KEY = os.getenv("MOTION_API_KEY")
if not API_KEY:
    sys.exit("❌  Set MOTION_API_KEY environment variable.")
HEADERS = {"X-API-Key": API_KEY}
BASE_URL = "https://api.usemotion.com/v1"

# ─────────────────────────────────────────────────────────────────────────────
# OmniFocus: folders → projects map
# ─────────────────────────────────────────────────────────────────────────────
JXA = r"""
(() => {
  const of      = Application('OmniFocus');
  const folders = of.defaultDocument.folders(); // top‑level folders only

  return JSON.stringify(folders.map(f => ({
    name: f.name(),
    projects: f.flattenedProjects().map(p => p.name())
  })));
})();
"""

def omnifocus_structure() -> Dict[str, Set[str]]:
    """Return {folderName: {project names…}}."""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", JXA],
        text=True, capture_output=True, check=True
    )
    data: List[Dict] = json.loads(result.stdout)
    return {item["name"]: set(item["projects"]) for item in data}

# ─────────────────────────────────────────────────────────────────────────────
# Motion: workspaces → projects map
# ─────────────────────────────────────────────────────────────────────────────

def motion_structure() -> Dict[str, Set[str]]:
    """Return {workspaceName: {project names…}} covering all workspaces."""
    ws_resp = requests.get(f"{BASE_URL}/workspaces", headers=HEADERS, timeout=30)
    ws_resp.raise_for_status()
    ws_json = ws_resp.json()

    # Handle wrapper form {"workspaces": [...]} *or* plain list [...]
    workspaces = ws_json.get("workspaces") if isinstance(ws_json, dict) else ws_json

    mapping: Dict[str, Set[str]] = {}
    for ws in workspaces:
        wid, wname = ws.get("id"), ws.get("name")
        if not wid or not wname:
            continue  # skip malformed entries
        proj_resp = requests.get(
            f"{BASE_URL}/projects?workspaceId={wid}", headers=HEADERS, timeout=30
        )
        proj_resp.raise_for_status()
        project_json = proj_resp.json()
        project_list = project_json.get("projects", project_json)  # unwrap if needed
        mapping[wname] = {p.get("name") for p in project_list if p.get("name")}
    return mapping

# ─────────────────────────────────────────────────────────────────────────────
# Pretty print diffs
# ─────────────────────────────────────────────────────────────────────────────

def print_header(title: str):
    print(f"\n{title}\n" + "─" * len(title))


def main() -> None:
    of_map = omnifocus_structure()
    mo_map = motion_structure()

    of_folders = set(of_map)
    mo_workspaces = set(mo_map)

    print_header("Folder ↔ Workspace diff")
    both_fw        = sorted(of_folders & mo_workspaces)
    only_of_folder = sorted(of_folders - mo_workspaces)
    only_mo_ws     = sorted(mo_workspaces - of_folders)

    print(f"✅ Both ({len(both_fw)}): " + ", ".join(both_fw))
    print(f"➕ Only in OmniFocus ({len(only_of_folder)}): " + ", ".join(only_of_folder))
    print(f"➖ Only in Motion ({len(only_mo_ws)}): " + ", ".join(only_mo_ws))

    # Compare projects within matched pairs
    for name in both_fw:
        of_projects = of_map.get(name, set())
        mo_projects = mo_map.get(name, set())
        both_proj   = sorted(of_projects & mo_projects)
        only_of_proj= sorted(of_projects - mo_projects)
        only_mo_proj= sorted(mo_projects - of_projects)

        print_header(f"Projects in '{name}' (matched folder/workspace)")
        print(f"  ✅ Both ({len(both_proj)}):")
        for p in both_proj: print("    •", p)
        print(f"  ➕ Only in OmniFocus ({len(only_of_proj)}):")
        for p in only_of_proj: print("    •", p)
        print(f"  ➖ Only in Motion ({len(only_mo_proj)}):")
        for p in only_mo_proj: print("    •", p)

if __name__ == "__main__":
    main()
