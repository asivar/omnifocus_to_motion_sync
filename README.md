# OmniFocus to Motion Sync

Bidirectional synchronization between OmniFocus and Motion task management systems.

## Features

- **Bidirectional sync** — OmniFocus tasks sync to Motion; Motion completions and new tasks sync back to OmniFocus
- **ID-based task matching** — robust bidirectional mapping by task ID (not name)
- **Sequential project support** — sequence position and blocked-by metadata in task descriptions
- **Structured logging** — rotating log files with correlation IDs (`~/Library/Logs/OmniFocusMotionSync/`)
- **State backups** — automatic backup + SHA-256 checksum before every save
- **Health monitoring** — separate health check script with macOS notifications
- **Dry-run mode** — preview all changes without making mutations
- **Sync history** — JSON-lines log of every sync run (`sync_history.jsonl`)
- **External configuration** — JSON config for workspace mappings, schedules, ignored folders

## Quick Setup

### 1. Install Dependencies

```bash
./setup.sh
```

Or manually:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API Key

Add to `~/.zshrc` or `~/.bash_profile`:

```bash
export MOTION_API_KEY='your_api_key_here'
```

Then reload:

```bash
source ~/.zshrc
```

### 3. Generate Configuration

```bash
source venv/bin/activate
python3 setup_config.py
```

This will fetch your Motion workspaces and create `config.json` with mappings. Or copy the example and edit manually:

```bash
cp config.example.json config.json
```

### 4. Run Initial Sync

```bash
source venv/bin/activate
python3 sync_of_to_motion.py --refresh-mapping
```

## Command Line Options

| Flag | Description |
|------|-------------|
| *(default)* | Bidirectional sync (OF → Motion, then Motion → OF) |
| `--sync-only` | Unidirectional sync only (OF → Motion) |
| `--refresh-mapping` | Force refresh Motion data mapping from API, then sync |
| `--mapping-only` | Only create/refresh the local mapping file, skip sync |
| `--dry-run` | Preview changes without making any mutations |
| `--config <file>` | Use custom config file (default: `config.json`) |

## Configuration

### Workspace Mapping

Maps OmniFocus folder names to Motion workspace names:

```json
{
  "workspace_mapping": {
    "OmniFocus Folder Name": "Motion Workspace Name"
  }
}
```

### Workspace Schedules

Motion's auto-scheduling feature uses these:

```json
{
  "workspace_schedules": {
    "Personal": "Personal hours",
    "Work": "Work hours"
  }
}
```

### Ignored Folders

OmniFocus folders to exclude from sync:

```json
{
  "ignored_folders": ["Routines", "Reference", "Archive"]
}
```

### Validate Config

```bash
python3 setup_config.py validate
```

## Automated Sync (LaunchAgent)

### Install Sync Agent

```bash
# Create log directory
mkdir -p ~/Library/Logs/OmniFocusMotionSync

# Copy example plist and update paths + API key
cp com.yourusername.omnifocus-motion-sync.example.plist \
   ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-sync.plist

# Edit the plist to set your paths and API key
# NOTE: LaunchAgents don't expand ~ — use full /Users/YOUR_USERNAME/... paths

# Load the agent
launchctl load ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-sync.plist
```

### Health Monitoring

A separate health check script monitors sync health and sends macOS notifications on issues.

```bash
# Copy example plist and update paths
cp com.yourusername.omnifocus-motion-healthcheck.example.plist \
   ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-healthcheck.plist

# Edit the plist to set your paths

# Load the agent (runs every 30 minutes)
launchctl load ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-healthcheck.plist
```

### Check Status

```bash
# Check if agents are running
launchctl list | grep omnifocus

# View sync logs
tail -f ~/Library/Logs/OmniFocusMotionSync/sync.log

# View health check logs
tail -f ~/Library/Logs/OmniFocusMotionSync/healthcheck.log
```

### Unload Agents

```bash
launchctl unload ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-sync.plist
launchctl unload ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-healthcheck.plist
```

## Files

| File | Description |
|------|-------------|
| `sync_of_to_motion.py` | Main sync script (bidirectional) |
| `health_check.py` | Health monitoring script |
| `config.example.json` | Example configuration (copy to `config.json`) |
| `com.yourusername.omnifocus-motion-sync.example.plist` | Example LaunchAgent for sync |
| `com.yourusername.omnifocus-motion-healthcheck.example.plist` | Example LaunchAgent for health checks |
| `setup_config.py` | Interactive config generator |
| `list_motion_workspaces.py` | List your Motion workspaces |
| `compare_projects.py` | Compare OmniFocus and Motion project states |
| `remove_of_duplicates.py` | Remove duplicate tasks in OmniFocus |
| `requirements.txt` | Python dependencies |
| `setup.sh` | Setup script |

## Troubleshooting

### Script Won't Run

Make sure virtual environment is activated:

```bash
source venv/bin/activate
```

### Lock File Issues

If the script says "another instance is running" but it's not:

```bash
rm /tmp/of2motion.lock
```

### Config Not Loading

Validate your config:

```bash
python3 setup_config.py validate
```

### Workspace Not Found

Run this to see your actual Motion workspaces:

```bash
python3 list_motion_workspaces.py
```

Then update `config.json` with the correct names.

### Check Logs

```bash
tail -n 50 ~/Library/Logs/OmniFocusMotionSync/sync.log
```
