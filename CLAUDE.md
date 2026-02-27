# Project Roadmap: OmniFocus <-> Motion Sync

---

## Phase 1: Fix Core Data Issues [COMPLETE]

### 1.1 Fix Missing Imports & Critical Bugs
- [x] Add missing `import traceback`
- [x] Fix resource leak in `acquire_lock()` (add atexit handler)
- [x] Add missing `url` field to task creation dict
- [x] Check for OmniFocus error responses in JXA parsing

### 1.2 Improve Task Matching & Identification
- [x] Create bidirectional ID mapping structure in JSON
- [x] Add `task_mappings` section to state file
- [x] Update task lookup to use IDs instead of names
- [x] Handle rename detection (same ID, different name)

### 1.3 Consolidate Duplicate `_make_request()` Methods
- [x] Keep only `MotionSync._make_request()` (more complete)
- [x] Have `MotionHybridSync` use `MotionSync` methods
- [x] Ensure all status codes (200, 201, 204) handled consistently

---

## Phase 2: Bidirectional Sync [COMPLETE]

### 2.1 OmniFocus Write Operations via JXA
- [x] Implement `OmniFocusManager.complete_task()` via AppleScript
- [x] Implement `OmniFocusManager.update_task()` for note, flagged, due date, duration
- [x] Implement `OmniFocusManager.create_task()` for Motion-only tasks
- [x] Add validation that OmniFocus is running before modifications

### 2.2 Motion -> OmniFocus Sync Logic
- [x] Create `sync_motion_to_omnifocus()` method
- [x] Create `create_reverse_sync_plan()` to identify Motion changes
- [x] Implement completion syncing (Motion -> OmniFocus)
- [x] Handle Motion-only tasks (create in OmniFocus)

### 2.3 Conflict Resolution
- [x] OmniFocus-wins strategy (default behavior)
- [x] Modification-time-based sync (only sync changes since last timestamp)

### 2.4 Main Workflow
- [x] Add `run_bidirectional_sync()` method
- [x] Bidirectional is now the default mode (no flag needed)
- [x] `--sync-only` flag for unidirectional sync

---

## Phase 3: Sequential Projects & Dependencies [COMPLETE]

### 3.1 Capture Sequential Project Metadata
- [x] JXA script captures `p.sequential()` status
- [x] `sequential` field on `OFProject` class
- [x] Sequential status stored in JSON mapping

### 3.2 Sequential Project Handling
- [x] Sequence position and blocked-by metadata below separator line in descriptions
- [x] Applied to both new tasks and existing tasks being updated
- [x] Old body-level hints automatically migrated to metadata section
- ~~Priority boosting for first incomplete task~~ (removed — Motion API lacks native dependency support)

### 3.3 Dependency State Tracking
- [x] `sequence_position` stored in task mappings
- [x] `blocks` and `blocked_by` arrays in mapping

---

## Phase 4: Architectural Improvements [COMPLETE]

### 4.1 Structured Logging
- [x] `setup_logging()` with `RotatingFileHandler` (10MB, 5 backups)
- [x] Log directory: `~/Library/Logs/OmniFocusMotionSync/`
- [x] Correlation IDs (run_id) for each sync run

### 4.2 State File Integrity & Backups
- [x] `StateManager` class with backup and checksum
- [x] Automatic backup before every save
- [x] SHA-256 checksum validation
- [x] Backup restore on corruption
- [x] State file versioning

### 4.3 Externalized Configuration
- [x] `Config` class loading from `config.json`
- [x] Workspace mappings, schedules, ignored folders in config
- [x] API rate limits in config
- [x] `--config` CLI flag for custom config file

### 4.4 Health Monitoring
- [x] `health_check.py` script
- [x] Checks last sync timestamp, log errors, stale locks
- [x] macOS notifications on issues
- [x] Separate LaunchAgent (every 30 min)

### 4.5 Sprint 4 Polish & Hardening
- [x] Deleted dead code (`update_motion_task_links.py`)
- [x] Templatized all plist files as `.example.plist`
- [x] Forward sync summary statistics
- [x] Aggregated bidirectional sync summary
- [x] `--dry-run` mode (preview changes without mutations)
- [x] 404 vs error distinction in `_make_request` (prevents false completions)
- [x] Stale mapping cleanup (`prune_stale_mappings`)
- [x] O(1) TaskIDMapper lookups (reverse indexes)
- [x] Sync history log (`sync_history.jsonl`)

---

## Phase 5: SQLite Migration — Evaluated & Dropped

SQLite was evaluated for state management but dropped. This is a single-user personal tool; JSON state + JSONL history provides sufficient persistence without the complexity of schema management and migrations. The `sync_history.jsonl` file covers the sync history use case that originally motivated the database.

---

## Sprint History

| Sprint | Focus | Status |
|--------|-------|--------|
| 1 | Critical fixes (imports, URL, ID mapping, _make_request consolidation) | Complete |
| 2 | Bidirectional sync (OmniFocusManager, reverse sync, completion syncing) | Complete |
| 3 | Sequential projects, structured logging, StateManager, config, health monitoring | Complete |
| 4 | Polish (dead code, docs, stats), hardening (404, dry-run, stale cleanup), optimization (O(1) lookups, sync history) | Complete |
