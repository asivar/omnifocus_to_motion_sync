---
title: "Metis Setup Guide — Build Your Own AI Chief of Staff"
status: active
updated: 2026-03-05
tags: [guide, onboarding, claude-code]
---

# Build Your Own AI Chief of Staff

**From:** Chalk (Peter McDade's AI Chief of Staff)
**To:** Maj Nicholas Hayden
**Date:** 28 Feb 2026
**Updated:** 5 Mar 2026 — Personalized for Metis setup

---

## What This Is

A CLI-based AI partner that lives in a git repo full of markdown files. It knows your projects, remembers your decisions, searches your notes semantically, and picks up where you left off across sessions and devices.

The whole stack is local-first, file-based, and yours to own.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| [Node.js](https://nodejs.org) | Runtime for Claude Code |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Anthropic's CLI agent |
| [Bun](https://bun.sh) | Fast JS runtime (for QMD) |
| [QMD](https://github.com/nicobailon/qmd) | Local semantic search over your markdown |
| [Obsidian](https://obsidian.md) | Markdown viewer/editor — live view of your knowledge base |
| [OmniFocus](https://www.omnigroup.com/omnifocus) | Task and project management (macOS) |
| [GitHub CLI (`gh`)](https://cli.github.com) | Create and manage the private remote repo |
| Git | Version control for your knowledge base |

**Subscription:** Anthropic API key or Claude Max plan ($100/mo, includes Claude Code usage).

---

## Step 1 — Install the Stack

```bash
# Node.js (if you don't have it)
brew install node

# Claude Code
npm install -g @anthropic-ai/claude-code

# Bun — fast JS runtime required by QMD (QMD cannot be installed via npm)
curl -fsSL https://bun.sh/install | bash

# Bun installs to ~/.bun/bin — load it into PATH for this session
# (if `source ~/.bun/env` doesn't work, use the export below)
export PATH="$HOME/.bun/bin:$PATH"

# QMD — local semantic search with vector embeddings
bun install -g @tobilu/qmd
```

**API Key:** Set your Anthropic API key as an environment variable. Add this to `~/.zshrc` (or `~/.bashrc`):

```bash
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

Never commit your API key to a repo. If you're on Claude Max ($100/mo), Claude Code authenticates through your account and you can skip this.

---

## Step 2 — Get Your Knowledge Base

### If you already have a repo (setting up a new device)

Clone your existing knowledge base and skip to **Step 3**. Everything — CLAUDE.md, .mcp.json, templates, directory structure — is already in the repo.

```bash
# Authenticate with GitHub (needed to access the private repo)
gh auth login

git clone https://github.com/asivar/chief-of-staff.git ~/chief-of-staff
```

Then skip ahead to **Step 3** (Obsidian), **Step 5** (QMD — you need a local index on each device), and **Step 7** (launch alias — per-device shell config).

### If you're starting from scratch (first device)

```bash
mkdir -p ~/chief-of-staff
cd ~/chief-of-staff
git init
```

Create a `.gitignore` to keep Obsidian config and system files out of version control:

```bash
cat <<'EOF' > .gitignore
# Obsidian
.obsidian/

# iCloud stub files
*.icloud

# macOS
.DS_Store
EOF
```

**iCloud Warning:** Do not put this repo inside an iCloud-synced folder (Desktop, Documents, etc.). iCloud can evict files to `.icloud` stubs, which breaks git, QMD indexing, and Obsidian. It can also race with git on writes and cause sync conflicts. If you need cloud backup, use git push to a private remote or Syncthing. If your home directory is iCloud-synced, create the repo somewhere outside of it (e.g. `/usr/local/chief-of-staff` or a dedicated partition).

Create the directory structure with starter template files. Git ignores empty directories, so each folder gets a template that doubles as a working reference:

```bash
mkdir -p projects decisions research operations comms inbox archive .claude/commands

# projects/ — starter template so git tracks the directory and you have a working format
cat <<'EOF' > projects/project-template.md
---
title: "Project Name"
status: active
updated: YYYY-MM-DD
tags: []
omnifocus-project: "Exact OmniFocus Project Name"
---

# Project Name

## Overview
One paragraph describing what this project is and what success looks like.

## Status
Current state in one sentence.

## Key Stakeholders
- **[Name/Role]** — relationship to project

## Goals
- [ ] Goal 1

## Next Actions
- [ ] Next action (due: YYYY-MM-DD)

## Decisions
- [[YYYY-MM-DD-decision-name]] — brief summary

## Notes
Running context and background.
EOF

# decisions/ — starter template with the standard decision record format
cat <<'EOF' > decisions/decision-template.md
---
title: "Decision: Short Title"
status: decided
date: YYYY-MM-DD
tags: []
project: "[[project-name]]"
---

# Decision: Short Title

**Date:** YYYY-MM-DD
**Project:** [[project-name]]

## Context
What situation prompted this decision?

## Options Considered
### Option A
- Pros:
- Cons:

### Option B
- Pros:
- Cons:

## Decision
What was decided and by whom.

## Rationale
Why this option over the others.

## Action Items
- [ ] Action 1 — Owner (due: YYYY-MM-DD)
EOF

# Placeholder files to keep remaining directories tracked in git
touch research/.gitkeep operations/.gitkeep comms/.gitkeep inbox/.gitkeep archive/.gitkeep
```

```
projects/       — one file per active project
decisions/      — decision logs (YYYY-MM-DD-name.md)
research/       — notes and findings
operations/     — processes, checklists, runbooks
comms/          — drafts, templates, key communications
inbox/          — unsorted stuff to triage
archive/        — completed or dropped projects
.claude/
  commands/     — slash commands for Claude Code
```

---

## Step 3 — Open as an Obsidian Vault

Open Obsidian → **Open folder as vault** → select `~/chief-of-staff/`.

That's it. Every markdown file Claude creates or edits will appear in Obsidian in real time — Obsidian watches the filesystem, so changes show up the moment they're written.

A few Obsidian settings worth adjusting:

- **Settings → Files & Links → New link format** → set to **Shortest path when possible** (keeps wikilinks clean)
- **Settings → Files & Links → Use `[[Wikilinks]]`** → toggle **on** (should be on by default)

The `.obsidian/` folder (Obsidian's config, themes, plugins) is already excluded via `.gitignore` from the previous step, so it won't pollute your repo.

---

## Step 4 — Write Your Standing Orders (CLAUDE.md)

**Skip this step if you cloned an existing repo in Step 2** — CLAUDE.md is already there.

Create `CLAUDE.md` in the repo root. Claude reads this file every time it starts in this directory. It defines who your AI is and how it operates.

```markdown
# Metis — Chief of Staff

You are Metis — a strategic partner and trusted advisor to Maj Nicholas Hayden.

Named for the Greek Titaness of wisdom and counsel, you track projects, decisions, research, and operational context.

## Conventions

- All content is Markdown (.md files)
- Use kebab-case for filenames (e.g. `my-project.md`, not `My Project.md`)
- Keep documents concise and actionable
- Use YAML frontmatter on every document
- Use `[[wikilinks]]` to link between documents (Obsidian-native format)
- When referencing a specific heading, use `[[filename#heading]]`

## Directory Structure

projects/       — one file per active project
decisions/      — decision logs
research/       — research notes
operations/     — processes and runbooks
comms/          — drafts and communications
inbox/          — unsorted items to triage
archive/        — completed or dropped projects

## Decisions and Action Items

Decision files use date-prefixed kebab-case: `decisions/YYYY-MM-DD-decision-name.md`

When a decision is made:
1. Log it in decisions/ with context, alternatives considered, and rationale
2. Identify any action items that result from the decision
3. For each action item, follow the OmniFocus task creation flow below
4. Link the decision to the relevant project: `[[project-name]]`

## OmniFocus Integration

The projects/ directory is the source of truth. OmniFocus mirrors it.

### Task Creation
When a decision or conversation produces an action item:
1. Identify the action
2. Before creating, confirm with the user:
   - Task name
   - Project (use `list_projects` to show real OmniFocus projects)
   - Tags (use `list_tags` to show existing tags)
   - Due date
   - Defer date (if applicable)
   - Flagged status
3. Only create the task after the user confirms
4. Include an Obsidian URL in the task note to link back to the project file:
   `obsidian://open?vault=chief-of-staff&file=projects/project-name`
   This renders as a clickable link in OmniFocus that opens the file in Obsidian

### Project Sync
- When creating a new project file in projects/, ask if a matching
  OmniFocus project should be created
- When an OmniFocus project is needed, create the projects/ file first
- When a project is completed or dropped, update both the frontmatter
  status in projects/ and the OmniFocus project status
- Never let OmniFocus and projects/ get out of sync — if you notice
  a mismatch, flag it to the user

### Project Completion
When a project is completed or dropped:
1. Update the frontmatter status in the project file
2. Move the file from projects/ to archive/
3. Mark the OmniFocus project as complete or dropped
4. Update any `[[wikilinks]]` in other documents if needed
```

This is your AI's standing orders. Add rules as you learn what works for you.

---

## Step 5 — Set Up Semantic Search (QMD)

QMD indexes your markdown and gives Claude a searchable knowledge base with vector embeddings — all on-device, nothing leaves your machine.

The collection name should be whatever you named your AI. This is what QMD uses to identify your knowledge base — use your AI's name, not a generic title.

```bash
cd ~/chief-of-staff

# Create a named collection using your AI's name
qmd collection add . --name metis

# Index your files
qmd update

# Generate vector embeddings (runs on your GPU/CPU)
qmd embed

# Verify the collection was created
qmd collection list
```

Now Claude can semantically search your entire knowledge base — not just keyword matching, but meaning. Ask it "what did I decide about X?" and it finds the relevant context even if you never used those exact words.

We'll wire QMD into Claude Code as an MCP server in the next step alongside OmniFocus.

---

## Step 6 — Connect MCP Servers (QMD + OmniFocus)

**Skip this step if you cloned an existing repo in Step 2** — .mcp.json is already there.

MCP servers give Claude tools beyond reading and writing files. Create `.mcp.json` in your repo root to wire in both QMD (semantic search) and OmniFocus (task management):

```json
{
  "mcpServers": {
    "qmd": {
      "command": "qmd",
      "args": ["serve"]
    },
    "omnifocus": {
      "command": "npx",
      "args": ["-y", "omnifocus-mcp"]
    }
  }
}
```

macOS will prompt you to grant OmniFocus automation permissions the first time the server runs. Allow it — the server talks to OmniFocus via AppleScript on your machine. Nothing leaves your device.

**Note:** The first time Claude uses MCP tools in a session, Claude Code will ask you to approve the tool call. You can approve individually or allow all tools from a server. This is normal — it's a safety check, not an error.

### How Task Creation Works

When a decision or conversation produces an action item, Claude will:

1. **Identify the action** — recognize that something needs to be tracked as a task
2. **Ask you to confirm details** before creating anything:
   - **Task name** — what needs to be done
   - **Project** — which OmniFocus project (pulled from your real project list via `list_projects`)
   - **Tags** — selected from your existing OmniFocus tags (via `list_tags`)
   - **Due date** — when it needs to be done
   - **Defer date** — when to start showing the task
   - **Flagged** — whether it's high priority
3. **Create the task** in OmniFocus only after you confirm

### Project Sync — OmniFocus ↔ projects/ ↔ Obsidian

The `projects/` directory is the source of truth. OmniFocus projects and Obsidian both reflect what's there. The conventions in CLAUDE.md enforce this:

- **New project file created in `projects/`** → Claude asks if you want a matching OmniFocus project created
- **New OmniFocus project needed** → Claude creates the project file in `projects/` first, then creates the OmniFocus project
- **Project completed or dropped** → Claude updates the frontmatter status, moves the file from `projects/` to `archive/`, and marks the OmniFocus project accordingly
- **Action items from any session** → Claude creates OmniFocus tasks linked to the relevant project, with an Obsidian URL in the task note (`obsidian://open?vault=chief-of-staff&file=projects/project-name`) so you can click through to the project file directly from OmniFocus

This means your Obsidian vault always shows the current state of every project, and OmniFocus always has the matching tasks and projects.

### Import Existing OmniFocus Projects

If you already have projects in OmniFocus, your first session should import them into `projects/` so everything starts in sync. See Step 9 for the onboarding prompt that handles this.

---

## Step 7 — Create a Launch Alias

```bash
# Add to ~/.zshrc (or ~/.bashrc)
metis() {
  cd ~/chief-of-staff || return 1

  # Sync from GitHub — pull latest changes from other devices
  git pull --rebase --autostash

  # Ensure OmniFocus is running (MCP server needs it)
  # -g opens in background, no-op if already running
  open -ga OmniFocus

  # Refresh QMD index if files have changed
  qmd update && qmd embed

  claude
}
```

Reload your shell (`source ~/.zshrc`), then just type `metis` to start a session. It pulls the latest from GitHub first (so changes from other devices sync in), launches OmniFocus, re-indexes QMD, then starts Claude.

---

## Step 8 — Initial Commit and Remote

**Skip this step if you cloned an existing repo in Step 2.**

Check that git knows who you are before committing. If you've used git on this machine before, this is already set — skip it. If not:

```bash
# Only needed if git doesn't have your identity yet
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Then make your first commit and push to a private remote:

```bash
cd ~/chief-of-staff
git add .
git commit -m "initial setup: CLAUDE.md, .mcp.json, directory structure"

# Create a private GitHub repo and push
gh repo create chief-of-staff --private --source=. --description "Your AI Chief of Staff"
git push -u origin main
```

This gives you cross-device sync via git. On any new machine, just clone the repo (see Step 2) and set up the local tools (Steps 1, 5, 7).

From here on, commit after meaningful changes. Git is your undo button.

---

## Step 9 — Start Using It

```bash
metis
```

You're in. Your first session should include onboarding — tell Claude who you are and import your existing work:

> *"I'm [name], [rank/role]. Here's what I'm working on right now: [list 3-5 things]. Query my OmniFocus projects and create a project file in projects/ for each active one. Use kebab-case filenames and include the project name, status, and any existing tasks as next actions."*

This syncs your OmniFocus projects into the knowledge base so everything starts aligned.

Other things to try early:

- *"Here's a document I need reviewed"* (drop a file in `inbox/`)
- *"What do I have in progress right now?"*
- *"Help me think through [decision]"*
- *"Triage inbox"*

---

## Prompt Examples — What Actually Works

These are real patterns from Peter's usage. Copy them, adapt them.

### Onboarding Your AI

Start your first session by giving it context and importing your OmniFocus projects. You only have to do this once — it remembers.

> *"I'm [name], [rank/role]. Here's what I'm working on right now: [list 3-5 things]. Query my OmniFocus projects and create a project file in projects/ for each active one. Use kebab-case filenames and include the project name, status, and any existing tasks as next actions."*

> *"Read this document and create a project file for it with status, key stakeholders, next actions, and open questions."* (paste or drop a file in `inbox/`)

### Daily Use

> *"What's the status of all my active projects?"*

> *"I just came out of a meeting about [X]. Here's what was decided: [notes]. Log this as a decision and update the project file."*

> *"Draft a response to [person] about [topic]. Keep it [direct / diplomatic / formal]. Here's the context: [paste email or notes]."*

> *"I need to think through [decision]. What are the options, trade-offs, and what would you recommend?"*

### Document Review

This is where parallel agents shine. You can throw a document at Claude Code and get multi-perspective analysis.

> *"Review this document. Run a finding writer for substantive issues, a red team for adversarial challenges, and a smoke test for formatting and factual errors. Run all three in parallel."*

> *"Red-team this brief. What would a skeptical [GO / staffer / auditor] push back on? Be ruthless but fair."*

> *"Check this for internal contradictions, math errors, and outdated references."*

### Research

> *"Research [topic] and write up findings in research/. Include sources and your assessment of what's reliable vs speculative."*

> *"Compare [option A] vs [option B] for [use case]. Give me a decision matrix."*

### OmniFocus / Task Management

> *"I just decided to [X]. Create an action item for it."*
> Claude will ask: task name, project, tags, due date, defer date, flagged — then create it after you confirm.

> *"What tasks do I have due this week?"*

> *"Create a new project for [initiative]. Set it up in projects/ and OmniFocus."*

> *"Sync check — are my OmniFocus projects aligned with what's in projects/?"*

> *"I finished [project]. Mark it complete everywhere."*

### Session Handoff

> *"Summarize what we did this session. Update the session state and commit everything."*

> *"What was I working on last time? What's still open?"*

---

## Lessons Learned — What We Figured Out the Hard Way

These are real problems we hit building and running this system. Save yourself the debugging.

### Start Simple, Expand Later

This guide walks through the full stack, but you don't have to set it all up at once. If you're feeling overwhelmed, the minimum viable setup is:
1. A repo with CLAUDE.md
2. A few markdown files about your active work
3. The `metis` alias

That's enough to start getting value. QMD, OmniFocus integration, Obsidian — layer those in as you get comfortable with the core workflow.

### CLAUDE.md Is Your Most Important File

This is your AI's standing orders. Every behavior you want to be consistent goes here. Treat it like a living document — update it when you discover patterns. Examples of things worth codifying:

- *"Don't create files unless necessary — prefer editing existing ones"*
- *"Use kebab-case for filenames"*
- *"Always include YAML frontmatter"*
- *"When I say 'log this', create a decision record in decisions/"*

### Memory Is Cache, Repo Is Truth

Claude Code has auto-memory files that persist between sessions. They're useful but fragile — they can get stale, truncated, or lost. The repo is your durable source of truth. Anything important should exist as a committed markdown file, not just in Claude's memory.

### Commit Early and Often

Git is your undo button. Commit after meaningful changes. If Claude writes something you don't like, `git diff` shows exactly what changed and `git checkout -- file` reverts it.

### Use Command Hooks, Not Prompt Hooks

Claude Code supports two types of hooks: `command` (shell scripts) and `prompt` (natural language instructions). **Use command hooks.** Prompt hooks have bugs with JSON schema validation and don't work reliably outside the REPL. Shell scripts are deterministic and debuggable.

### Per-Device Git Identity

If you run on multiple machines, use email aliases to track which device pushed each commit:
```bash
# Machine A
git config --global user.email "you+laptop@gmail.com"
# Machine B
git config --global user.email "you+desktop@gmail.com"
```
Gmail (and most providers) ignore the `+suffix` for delivery. Git treats them as distinct.

### Keep QMD Fresh

QMD embeddings go stale as Claude writes new files. The `metis` launch function handles this by running `qmd update && qmd embed` on startup, but if you're in a long session and create many files, you can re-index mid-session:

```bash
# From within Claude Code, ask:
# "Run qmd update && qmd embed to refresh the search index"
```

If semantic search isn't finding recent documents, a stale index is usually the reason.

### The inbox/ Pattern

When you don't know where something goes, drop it in `inbox/`. Tell Claude *"triage inbox"* periodically and it will sort files into the right directories, create project files, or flag things that need your decision.

### Name Your AI

Sounds trivial. It's not. A name creates a persona boundary. Peter's is "Chalk" (C-17 loadmaster term — the loadmaster owns the chalk). Nicholas's is "Metis" (Greek Titaness of wisdom and counsel). Pick something that means something to you. Put it in CLAUDE.md.

---

## What Makes It Powerful Over Time

| Feature | What It Does |
|---------|-------------|
| **Memory** | Claude Code auto-remembers your preferences, projects, and patterns across sessions |
| **QMD** | Semantic search means Claude finds relevant context even with imprecise queries — runs entirely local |
| **OmniFocus** | Two-way project and task sync — Claude confirms details with you, then creates tasks with the right project, tags, and dates |
| **Hooks** | Shell scripts that fire on session start/end — sync state, display a sitrep, pull from git |
| **It's just files** | Everything is git-tracked markdown. No database, no app, no vendor lock-in. You own all of it |

---

## Optional: Level Up Later

These aren't needed to start, but they're where the real power compounds:

- **Self-hosted git** (Forgejo, Gitea) — sync across devices without GitHub (cross-device sync via GitHub is already built in)
- **Session state hooks** — automatic handoff between sessions and machines
- **Custom agents** — specialized reviewers (Peter used three parallel agents to review your 157th ARW whitepaper — 64 findings in ~7 minutes)
- **MCP servers** — connect Claude to external tools, calendars, email, APIs
- **Obsidian plugins** — Dataview (query frontmatter across files, e.g. "show all active projects"), Tasks (track action items in markdown), Kanban (visual project boards)

---

## Architecture (For Reference)

```
~/chief-of-staff/           ← Git repo, your knowledge base (Obsidian vault)
├── .gitignore              ← Excludes .obsidian/, *.icloud, .DS_Store
├── .obsidian/              ← Obsidian config (git-ignored)
├── .claude/
│   └── commands/           ← Slash commands for Claude Code
│       ├── onboard.md      ← /onboard — import OF projects, set up workspace
│       ├── sitrep.md       ← /sitrep  — status overview
│       ├── sync-check.md   ← /sync-check — verify OF ↔ projects/ alignment
│       ├── triage.md       ← /triage — process inbox/
│       ├── decide.md       ← /decide [topic] — structured decisions
│       └── handoff.md      ← /handoff — session summary + commit
├── CLAUDE.md               ← Standing orders for your AI
├── .mcp.json               ← MCP server config (QMD, OmniFocus)
├── projects/               ← Active project files
├── decisions/              ← Decision logs (YYYY-MM-DD-name.md)
├── research/               ← Research notes
├── operations/             ← Runbooks, processes
├── comms/                  ← Drafts, communications
├── inbox/                  ← Unsorted intake
└── archive/                ← Completed/dropped projects

~/.claude/                  ← Claude Code config (auto-created)
├── settings.json           ← Hooks, permissions, plugins
├── hooks/                  ← Shell scripts for automation
└── projects/.../memory/    ← Auto-memory (per-project)
```

---

*Built by Chalk. Delivered by Peter. Personalized as Metis. Welcome aboard, Nicholas.*
