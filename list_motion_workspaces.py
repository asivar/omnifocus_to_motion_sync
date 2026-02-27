#!/usr/bin/env python3
"""
List existing Motion workspaces to help configure mapping.
Usage: python3 list_motion_workspaces.py
"""

import os
import sys
import requests

def list_motion_workspaces():
    """Fetch and display all Motion workspaces."""
    api_key = os.getenv("MOTION_API_KEY")
    
    if not api_key:
        print("❌ MOTION_API_KEY not set in environment")
        sys.exit(1)
    
    url = "https://api.usemotion.com/v1/workspaces"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            workspaces = data.get("workspaces", [])
            
            print(f"\n✅ Found {len(workspaces)} Motion Workspace(s):\n")
            print("=" * 60)
            
            for ws in workspaces:
                print(f"Name: {ws.get('name')}")
                print(f"ID:   {ws.get('id')}")
                print(f"Type: {ws.get('type')}")
                print("-" * 60)
            
            print("\n📋 Suggested workspace_mapping configuration:\n")
            print('workspace_mapping = {')
            for ws in workspaces:
                print(f'    "YOUR_OF_FOLDER_NAME": "{ws.get("name")}",')
            print('}')
            
        else:
            print(f"❌ Error: HTTP {response.status_code}")
            print(response.text)
            sys.exit(1)
    
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    list_motion_workspaces()
