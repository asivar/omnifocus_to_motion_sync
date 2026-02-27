#!/usr/bin/env python3
"""
Configuration helper for OmniFocus to Motion Sync
Helps you create and validate config.json
"""

import json
import os
import sys
import requests

def list_motion_workspaces(api_key: str):
    """Fetch Motion workspaces to help configure mapping."""
    url = "https://api.usemotion.com/v1/workspaces"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get("workspaces", [])
        else:
            print(f"❌ Error fetching workspaces: HTTP {response.status_code}")
            return []
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

def create_config_interactive():
    """Interactive config creation."""
    print("🔧 OmniFocus to Motion Sync - Configuration Setup")
    print("=" * 60)
    
    # Get API key
    api_key = os.getenv("MOTION_API_KEY")
    if not api_key:
        api_key = input("\n📍 Enter your Motion API key: ").strip()
        if not api_key:
            print("❌ API key required")
            sys.exit(1)
    
    print("\n🔍 Fetching your Motion workspaces...")
    workspaces = list_motion_workspaces(api_key)
    
    if not workspaces:
        print("⚠️  Could not fetch workspaces. Creating minimal config.")
        config = {
            "workspace_mapping": {},
            "workspace_schedules": {},
            "ignored_folders": ["Routines", "Reference"],
            "sync_settings": {
                "api_rate_limit_delay": 0.2,
                "workspace_processing_delay": 0.5,
                "default_due_date_offset_days": 14
            },
            "paths": {
                "lock_file": "/tmp/of2motion.lock",
                "state_file": "motion_hierarchical_mapping.json",
                "log_directory": "~/Library/Logs/OmniFocusMotionSync"
            },
            "sequential_project_handling": {
                "enabled": True
            }
        }
    else:
        print(f"\n✅ Found {len(workspaces)} workspace(s):\n")
        for i, ws in enumerate(workspaces, 1):
            print(f"  {i}. {ws.get('name')} (ID: {ws.get('id')})")
        
        # Create identity mapping for all workspaces
        workspace_mapping = {}
        workspace_schedules = {}
        
        print("\n📋 Creating workspace mappings...")
        print("   (OmniFocus folders will map to workspaces with the same name)\n")
        
        for ws in workspaces:
            ws_name = ws.get('name')
            workspace_mapping[ws_name] = ws_name
            
            # Suggest schedule based on name
            if "work" in ws_name.lower() or "office" in ws_name.lower():
                schedule = "Work hours"
            elif "home" in ws_name.lower() or "personal" in ws_name.lower():
                schedule = "Personal hours"
            else:
                schedule = "Anytime (24/7)"
            
            workspace_schedules[ws_name] = schedule
            print(f"   ✓ {ws_name} → Schedule: {schedule}")
        
        config = {
            "workspace_mapping": workspace_mapping,
            "workspace_schedules": workspace_schedules,
            "ignored_folders": ["Routines", "Reference"],
            "sync_settings": {
                "api_rate_limit_delay": 0.2,
                "workspace_processing_delay": 0.5,
                "default_due_date_offset_days": 14
            },
            "paths": {
                "lock_file": "/tmp/of2motion.lock",
                "state_file": "motion_hierarchical_mapping.json",
                "log_directory": "~/Library/Logs/OmniFocusMotionSync"
            },
            "sequential_project_handling": {
                "enabled": True
            }
        }
    
    # Save config
    config_file = "config.json"
    
    if os.path.exists(config_file):
        overwrite = input(f"\n⚠️  {config_file} already exists. Overwrite? (y/N): ").strip().lower()
        if overwrite != 'y':
            print("❌ Aborted.")
            sys.exit(0)
    
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"\n✅ Configuration saved to {config_file}")
        print("\nYou can now edit this file to customize:")
        print("  • Workspace mappings (if OF folder names differ from Motion)")
        print("  • Workspace schedules (for auto-scheduling)")
        print("  • Ignored folders")
        print("  • Sync settings (API rate limits, default due date offset)")
        print("  • Paths (lock file, state file, log directory)")
        print("  • Sequential project handling")
    except IOError as e:
        print(f"❌ Error saving config: {e}")
        sys.exit(1)

def validate_config(config_file: str = "config.json"):
    """Validate existing config file."""
    if not os.path.exists(config_file):
        print(f"❌ Config file '{config_file}' not found")
        sys.exit(1)
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        print(f"✅ Config file '{config_file}' is valid JSON\n")
        print("📋 Configuration Summary:")
        print(f"  Workspace Mappings: {len(config.get('workspace_mapping', {}))}")
        print(f"  Workspace Schedules: {len(config.get('workspace_schedules', {}))}")
        print(f"  Ignored Folders: {config.get('ignored_folders', [])}")
        sync = config.get('sync_settings', {})
        print(f"  API Rate Limit Delay: {sync.get('api_rate_limit_delay')}s")
        print(f"  Default Due Date Offset: {sync.get('default_due_date_offset_days', 'not set')} days")
        seq = config.get('sequential_project_handling', {})
        print(f"  Sequential Projects: {'enabled' if seq.get('enabled') else 'disabled'}")
        paths = config.get('paths', {})
        print(f"  Log Directory: {paths.get('log_directory', 'not set')}")
        
        return True
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in config file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error reading config: {e}")
        sys.exit(1)

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        validate_config()
    else:
        create_config_interactive()
