# OmniFocus to Motion Sync

Bidirectional synchronization between OmniFocus and Motion task management systems.

## 🚀 Quick Setup

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

Create your `config.json` file with workspace mappings:

```bash
source venv/bin/activate
python3 setup_config.py
```

This will:
- Fetch your Motion workspaces
- Create workspace mappings automatically
- Set up default schedules
- Save to `config.json`

Or copy the example and edit manually:

```bash
cp config.example.json config.json
```

See `config.example.json` for the template and the [Configuration](#-configuration) section below for details.

### 4. Run Initial Sync

```bash
source venv/bin/activate
python3 sync_of_to_motion.py --refresh-mapping
```

## 📋 Command Line Options

- `--sync-only` - Only run sync (uses existing local cache)
- `--refresh-mapping` - Force refresh Motion data mapping, then sync
- `--mapping-only` - Only create/refresh the local mapping file, skip sync
- `--config <file>` - Use custom config file (default: config.json)

## 🔧 Configuration

### Workspace Mapping

Maps OmniFocus folder names to Motion workspace names:

```json
{
  "workspace_mapping": {
    "OmniFocus Folder Name": "Motion Workspace Name"
  }
}
```

If names match exactly, use identity mapping:
```json
{
  "workspace_mapping": {
    "My Workspace": "My Workspace"
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

## 🔄 Automated Sync (LaunchAgent)

### Install LaunchAgent:

```bash
# Create log directory
mkdir -p ~/Library/Logs/OmniFocusMotionSync

# Copy example plist and update paths + API key
cp com.yourusername.omnifocus-motion-sync.example.plist ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-sync.plist

# Load the agent
launchctl load ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-sync.plist
```

### Check Status:

```bash
# Check if it's running
launchctl list | grep omnifocus

# View logs
tail -f ~/Library/Logs/OmniFocusMotionSync/sync.log
```

### Unload LaunchAgent:

```bash
launchctl unload ~/Library/LaunchAgents/com.yourusername.omnifocus-motion-sync.plist
```

## 📁 Files

- `sync_of_to_motion.py` - Main sync script
- `config.example.json` - Example configuration (copy to `config.json`)
- `com.yourusername.omnifocus-motion-sync.example.plist` - Example LaunchAgent
- `setup_config.py` - Interactive config generator
- `list_motion_workspaces.py` - List your Motion workspaces
- `requirements.txt` - Python dependencies
- `setup.sh` - Setup script

## 🐛 Troubleshooting

### Script Won't Run

Make sure virtual environment is activated:

```bash
source venv/bin/activate
```

### Lock File Issues

If script says "another instance is running" but it's not:

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

## 📊 Current Features (Sprint 1 Complete ✅)

- ✅ One-way sync: OmniFocus → Motion
- ✅ Create projects and tasks in Motion
- ✅ Update Motion tasks when OmniFocus changes
- ✅ Mark Motion tasks complete when done in OmniFocus
- ✅ Bidirectional ID mapping infrastructure
- ✅ File locking (prevents concurrent runs)
- ✅ Comprehensive error handling
- ✅ External configuration file (JSON)
- ✅ Configurable workspace mappings
- ✅ Configurable ignored folders

## 🚧 Coming Soon (Sprint 2)

- ⏳ Bidirectional sync: Motion → OmniFocus
- ⏳ Complete OmniFocus tasks when done in Motion
- ⏳ Conflict resolution
- ⏳ Sequential project handling

## 📝 Configuration Examples

### Multiple Workspaces

```json
{
  "workspace_mapping": {
    "OF Folder A": "Motion Workspace 1",
    "OF Folder B": "Motion Workspace 1",
    "OF Folder C": "Motion Workspace 2",
    "Side Projects": "Freelance"
  }
}
```

### Custom Schedules

```json
{
  "workspace_schedules": {
    "Work": "Work hours",
    "Personal": "Personal hours",
    "Freelance": "Anytime (24/7)",
    "Errands": "Weekend only"
  }
}
```

### Adjust Rate Limits

If you're hitting API limits, increase delays:

```json
{
  "sync_settings": {
    "api_rate_limit_delay": 0.5,
    "workspace_processing_delay": 1.0
  }
}
```
