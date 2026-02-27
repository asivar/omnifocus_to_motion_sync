# 📋 Project Roadmap: OmniFocus ↔ Motion Sync

---

## Phase 1: Fix Core Data Issues (Week 1) 🔴 HIGH PRIORITY

### 1.1 Fix Missing Imports & Critical Bugs
- [ ] Add missing `import traceback`
- [ ] Fix resource leak in `acquire_lock()` (add atexit handler)
- [ ] Add missing `url` field to task creation dict (line 593-599)
- [ ] Check for OmniFocus error responses in JXA parsing

### 1.2 Improve Task Matching & Identification

**Problem:** Tasks matched by name only → breaks with renames, duplicates overwrite

```python
# Current (fragile):
tasks[t_name] = t  # Overwrites if duplicate names

# New approach: Use composite keys
task_key = f"{of_id}::{motion_id}"  # Or hash of both IDs
```

- [ ] Create bidirectional ID mapping structure in JSON
- [ ] Add `task_mappings` section to state file:
```json
{
  "task_mappings": {
    "of_xyz123": "motion_abc456",
    "of_xyz124": "motion_abc457"
  }
}
```
- [ ] Update task lookup to use IDs instead of names
- [ ] Handle rename detection (same ID, different name)

### 1.3 Consolidate Duplicate `_make_request()` Methods

**Problem:** Both `MotionSync` and `MotionHybridSync` have separate implementations

- [ ] Keep only `MotionSync._make_request()` (more complete)
- [ ] Have `MotionHybridSync` use `MotionSync` methods or remove its version
- [ ] Ensure all status codes (200, 201, 204) handled consistently

---

## Phase 2: Add Bidirectional Sync (Week 2) 🟡 MEDIUM PRIORITY

### 2.1 Add OmniFocus Write Operations via JXA

Create new class to handle OmniFocus modifications:

```python
class OmniFocusManager:
    """Handles writing data back to OmniFocus via JXA."""
    
    @staticmethod
    def complete_task(task_id: str) -> bool:
        """Mark an OmniFocus task as complete."""
        pass
    
    @staticmethod
    def update_task(task_id: str, updates: Dict[str, Any]) -> bool:
        """Update OmniFocus task fields."""
        pass
    
    @staticmethod
    def create_task(project_id: str, task_data: Dict) -> Optional[str]:
        """Create new task in OmniFocus (for Motion-only tasks)."""
        pass
```

- [ ] Implement `complete_task()` JXA script
- [ ] Implement `update_task()` for name, note, flagged, due date
- [ ] Test JXA error handling
- [ ] Add validation that OmniFocus is running before modifications

### 2.2 Implement Motion → OmniFocus Sync Logic

```python
def sync_motion_to_omnifocus(self):
    """Sync Motion changes back to OmniFocus."""
    # 1. Load current Motion state
    # 2. Load OmniFocus structure  
    # 3. Compare using task_mappings
    # 4. Create reverse sync plan
    # 5. Execute updates to OmniFocus
    pass
```

- [ ] Create `sync_motion_to_omnifocus()` method
- [ ] Create `create_reverse_sync_plan()` to identify Motion changes
- [ ] Implement completion syncing (Motion ✓ → OmniFocus ✓)
- [ ] Implement field updates (priority, due date, duration)
- [ ] Handle Motion-only tasks (optionally create in OmniFocus)

### 2.3 Add Conflict Resolution

- [ ] Store both `last_of_update` and `last_motion_update` timestamps
- [ ] Implement conflict detection (both changed since last sync)
- [ ] Create `resolve_conflict()` method with strategy:
  - Option A: Last-write-wins (compare timestamps)
  - Option B: OmniFocus always wins (current behavior)
  - Option C: Prompt user (log conflicts to review)
- [ ] Add `--conflict-strategy` CLI argument

### 2.4 Update Main Workflow

```python
def run_bidirectional_sync(self):
    """Execute full bidirectional sync."""
    # Phase 1: OmniFocus → Motion
    self.sync_omnifocus_to_motion()
    
    # Phase 2: Motion → OmniFocus
    self.sync_motion_to_omnifocus()
    
    # Phase 3: Save final state
    self.save_motion_data_to_file(...)
```

- [ ] Add `run_bidirectional_sync()` method
- [ ] Add `--bidirectional` CLI flag
- [ ] Update LaunchAgent to use bidirectional mode
- [ ] Test round-trip: OF → Motion → OF

---

## Phase 3: Handle Sequential Projects & Dependencies (Week 3) 🟢 NICE-TO-HAVE

### 3.1 Capture Sequential Project Metadata

- [ ] Update JXA script to capture `p.sequential()` status
- [ ] Add `sequential` field to `OFProject` class
- [ ] Store sequential status in JSON mapping

### 3.2 Implement Sequential Project Handling

**Strategy:** Multi-pronged approach since Motion has no native dependencies

```python
def handle_sequential_project(self, project: OFProject):
    """Convert sequential projects into Motion-friendly format."""
    # Strategy 1: Visual indicators in task names
    # Strategy 2: Priority mapping (first task HIGH, rest MEDIUM)
    # Strategy 3: Staggered due dates
    # Strategy 4: Dependency notes in description
    pass
```

- [ ] Add visual sequence indicators (🟢 1️⃣, 🔵 2️⃣, etc.)
- [ ] Add "Blocked by" notes to task descriptions
- [ ] Map first incomplete task to HIGH priority
- [ ] Optionally stagger due dates based on duration
- [ ] Add project-level note: "⚠️ This is a sequential project"

### 3.3 Track Dependency State

- [ ] Store `sequence_position` for tasks in sequential projects
- [ ] Store `blocks` and `blocked_by` arrays in mapping
- [ ] Update these when tasks complete
- [ ] Consider updating priorities when blocker completes

---

## Phase 4: Architectural Improvements (Week 4) 🔵 STABILITY

### 4.1 Replace Print Statements with Structured Logging

- [ ] Create `setup_logging()` function
- [ ] Replace all `print()` calls with `logger.info()`, `logger.error()`, etc.
- [ ] Add log rotation (`RotatingFileHandler`, 10MB, 5 backups)
- [ ] Configure log directory: `~/Library/Logs/OmniFocusMotionSync/`
- [ ] Add correlation IDs to track individual sync runs

### 4.2 Add State File Integrity & Backups

- [ ] Create `StateManager` class
- [ ] Add automatic backup before save (`state.json.backup`)
- [ ] Add SHA256 checksum validation (`state.json.sha256`)
- [ ] Implement backup restore on corruption
- [ ] Add state file versioning

### 4.3 Externalize Configuration

- [ ] Create `config.py` with `Config` class
- [ ] Move hardcoded workspace schedule mapping to config
- [ ] Move ignored folders to config
- [ ] Add API rate limits to config
- [ ] Support optional config file override: `config.local.py`

### 4.4 Add Health Monitoring

- [ ] Create `health_check.py` script
- [ ] Check last sync timestamp (alert if > 2 hours)
- [ ] Check for excessive errors in logs
- [ ] Check for stale lock files
- [ ] Send macOS notifications on issues
- [ ] Create separate LaunchAgent for health checks (every 30 min)
- [ ] Optional: Integrate with healthchecks.io

---

## Phase 5: Database Migration (Optional - Week 5+) 🟣 FUTURE

### 5.1 SQLite State Management

- [ ] Create `DatabaseStateManager` class
- [ ] Design schema (`tasks`, `sync_history`, `conflicts` tables)
- [ ] Implement migration from JSON to SQLite
- [ ] Add sync history tracking
- [ ] Add conflict logging table
- [ ] Keep JSON export for debugging

### 5.2 Enhanced Querying & Reporting

- [ ] Add SQL queries for common operations
- [ ] Generate sync reports (tasks created/updated per day)
- [ ] Track sync performance metrics
- [ ] Build simple CLI tool to query sync history

---

## 🎯 Recommended Implementation Order

### Sprint 1: Critical Fixes (Days 1-3)
1. ✅ Fix missing imports (traceback, atexit)
2. ✅ Fix task URL not being passed
3. ✅ Add bidirectional ID mapping structure
4. ✅ Consolidate `_make_request()` methods

### Sprint 2: Motion → OmniFocus Completion Sync (Days 4-7)
5. ✅ Implement `OmniFocusManager.complete_task()`
6. ✅ Create basic `sync_motion_to_omnifocus()` (completions only)
7. ✅ Test round-trip completion sync
8. ✅ Add `--bidirectional` CLI flag

### Sprint 3: Full Bidirectional Updates (Week 2)
9. ✅ Implement remaining OmniFocus update operations
10. ✅ Add conflict resolution logic
11. ✅ Handle Motion-only tasks
12. ✅ Full testing of bidirectional flow

### Sprint 4: Sequential Projects (Week 3)
13. ✅ Capture sequential project metadata
14. ✅ Implement multi-strategy sequential handling
15. ✅ Test with real sequential projects

### Sprint 5: Architecture (Week 4)
16. ✅ Add structured logging
17. ✅ Add StateManager with backups
18. ✅ Externalize configuration
19. ✅ Add health monitoring

---

## 📊 Success Metrics

After completing the plan, you should have:

- ✅ Bidirectional sync (Motion completions → OmniFocus)
- ✅ Robust task matching (by ID, not name)
- ✅ Sequential project support (visual + priority hints)
- ✅ Conflict resolution (configurable strategy)
- ✅ Better logging (structured, rotated, searchable)
- ✅ State backups (automatic, with integrity checks)
- ✅ Health monitoring (automated alerts)
- ✅ Clean architecture (config separate from code)
