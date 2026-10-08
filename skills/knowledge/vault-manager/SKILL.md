---
name: vault-manager
description: Read, search, and update the ProxAI Obsidian vault. Understands folder structure, wiki links, templates, and data tiering rules.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [obsidian, vault, wiki, notes, knowledge]
    category: knowledge
    requires_toolsets: [terminal]
---

# Vault Manager — ProxAI Obsidian Vault

## Writing Wiki Pages
When you need to create or update a wiki page, use delegate_task to hand
off the writing. Do not write files directly. Always delegate write operations.

### delegate_task Model Override (REQUIRED)
Always use the claude-sonnet-4-6 model override for vault write tasks:
```python
delegate_task(
    goal="...",
    toolsets=["terminal", "file"],
    # model override injected via user memory preference
)
```
Per user preference: delegate vault write/research tasks using model override
`{ "model": "claude-sonnet-4-6", "provider": "anthropic" }`.
After all vault tasks complete, revert/remind user to switch back to qwen2.5:14b.

## When to Use
- User asks to read, find, search, or look up notes
- User asks to create, write, save, or log something to the vault
- User mentions vault, wiki, notes, obsidian, or any folder name (brain-dump, courses, daily, etc.)
- User says "save this", "remember this", "log this", "add to wiki"
- User asks "what do I know about X" or "summarize my notes on X"

## Procedure

### 1. Locate the vault
The vault is mounted at `/workspace/vault/`

### 2. Understand the folder structure

| Folder | Purpose | Access |
|---|---|---|
| `_templates/` | Note templates for consistent formatting | READ ONLY |
| `00-Daily/` | Daily notes and journal entries | READ ONLY |
| `01-Personal/` | PRIVATE — never read or reference unless explicitly asked | READ ONLY |
| `02-Projects/` | Active project documentation | READ ONLY |
| `03-Courses/` | Course notes and learning material | READ ONLY |
| `04-Knowledge/` | Reference knowledge and research | READ ONLY |
| `05-Brain-Dump/` | Quick captures, unprocessed thoughts | READ ONLY |
| `06-Focus-Logs/` | ADHD focus session records | READ ONLY |
| `07-Security/` | Security configs and audit logs | READ ONLY |
| `08-Roles/` | Role definitions and personas | READ ONLY |
| `09-Career/` | Career planning and job-related docs | READ ONLY |
| `10-Wiki/` | Knowledge wiki — interconnected reference pages | READ + WRITE |
| `11-OTS/` | OTS content | READ ONLY |
| `CLAUDE/` | Claude-specific context and logs | READ ONLY |

### 2b. Check installed plugins (before dashboards)
Before creating any Dataview or dashboard page, confirm Dataview is installed:
```bash
find /workspace/vault/.obsidian/plugins/ -maxdepth 1 -type d | sort
cat /workspace/vault/.obsidian/community-plugins.json
```
- If `dataview` is present → use Dataview TABLE/LIST queries freely
- If not → use static markdown tables only (no dynamic queries)

Known installed plugins (as of 2026-05): `templater-obsidian`, `dataview`, `obsidian-kanban`, `calendar`, `quickadd`

### 3. Search the vault
```bash
# Search across the entire vault for a topic
grep -ri "search term" /workspace/vault/ --include='*.md' -l

# Search with context
grep -ri "search term" /workspace/vault/ --include='*.md' -C 2

# Search only in a specific folder
grep -ri "search term" /workspace/vault/03-Courses/ --include='*.md'

# List all files in a folder
find /workspace/vault/04-Knowledge/ -name "*.md" | head -20
```

### 4. Write to the wiki (10-Wiki ONLY)
Use kebab-case filenames. Always include YAML frontmatter:
```markdown
---
title: Topic Name
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [tag1, tag2]
aliases: [alternate name]
---

# Topic Name

Content here. Link to related pages with [[Other Page Name]].
```

### 5. Update existing wiki pages
Always read the page first, then update the `updated:` date in frontmatter before writing.

### 6. Link related content
When creating wiki pages, search for related existing pages and add `[[wiki links]]` to connect them.

## Research Workflow
When the user asks to research a topic, learn about something, or wants new information added to the wiki:

1. **Search the vault first** — check if existing notes already cover the topic
```bash
   grep -ril "topic" /workspace/vault/ --include="*.md"
```

2. **Search the web** for current information using web_search and web_extract tools

3. **Synthesize** — combine what's already in the vault with new information from the web

4. **Write to wiki** — create or update a page in `10-Wiki/` with:
   - Proper frontmatter (title, created, updated, tags)
   - Summary of findings in your own words
   - `[[wiki links]]` to any related existing vault pages
   - Sources section at the bottom listing URLs referenced

### Triggers for research mode:
- "research X", "look up X", "learn about X", "what is X"
- "add X to my wiki", "update the wiki on X"
- "find out about X and save it"

## Dashboard Creation Workflow

When the user asks for a dashboard for wiki content:

1. **Check plugins first** (see step 2b above) — determines Dataview vs static approach
2. **Create a `*-dashboard.md` file in `10-Wiki/`** — kebab-case, same frontmatter rules
3. **Dashboard sections to include:**
   - Manual progress checklist (`- [ ]` tasks) for curricula/course content
   - Dataview TABLE query filtered by tag (e.g. `WHERE contains(tags, "topic-tag")`)
   - Quick reference cheat sheet (static bullets — one key rule per lesson)
   - Reusable prompt/template scaffold in a plain code block
   - Dataview LIST of related pages by tag
   - Footer links to related wiki pages
4. **Dataview code fence syntax:** use ` ```dataview ``` ` — never nest inside other blocks
5. **Do NOT put prompt templates inside Dataview blocks** — they get interpreted as queries

### Dashboard template pattern
```markdown
---
title: Topic — Dashboard
cssclass: dashboard
tags: [topic, dashboard]
---

# 🧠 Topic Dashboard
> [[topic-index]] — back to curriculum

## ✅ Progress
- [ ] [[lesson-1|Lesson 1 — Name]]
- [ ] [[lesson-2|Lesson 2 — Name]]

## 📋 All Lessons
\```dataview
TABLE title AS "Lesson", file.mtime AS "Last Visited"
FROM "10-Wiki"
WHERE contains(tags, "topic-tag") AND file.name != "topic-index" AND file.name != "topic-dashboard"
SORT file.name ASC
\```

## ⚡ Quick Reference
- **Lesson 1:** Key rule in one sentence.

## 🧪 Starter Template
\```
[reusable template content here — plain code block, not dataview]
\```
```

## Curriculum Creation Workflow

When asked to create a multi-lesson curriculum (e.g. "teach me X using the wiki"):

1. **Search vault first** for existing source material on the topic
2. **Create an index page** (`topic-101.md`) listing all lessons with `[[wiki links]]`
3. **Delegate each lesson** as a batch via a single delegate_task call describing all files
4. **Lesson structure:** Key Takeaways blockquote + before/after examples + Practice Exercise
5. **Link lessons as a chain:** each lesson links to prev/next + back to index
6. **Follow up with a dashboard** (`topic-101-dashboard.md`) after curriculum is created

Lesson file naming: `topic-101-lesson-N-short-name.md` (kebab-case, zero-padded N)

## Vault Host Path

The vault is hosted on the machine at: `/home/prox/Documents/ProxVault`
Docker mounts it as: `/workspace/vault/` (read-only root, `10-Wiki/` is read-write)

> **Note:** Early skill versions referenced `/workspace/vault/` — that path does not exist. The correct mount point is `/workspace/vault/`.

## Pitfalls
- **NEVER read `01-Personal/` unless the user explicitly asks.** This folder is private.
- **Only write to `10-Wiki/`.** All other folders are read-only. If asked to save elsewhere, offer to save to the wiki instead.
- **Check `_templates/` first** before creating notes to see if a template exists for that type.
- **Never reproduce full notes** — summarize and cite which files contain the information.
- **Always use wiki link syntax** `[[Page Name]]` when referencing other notes in wiki pages.
- **CLAUDE/ folder does not exist** — the skill references it but it was not present in vault as of 2026-05. Don't expect it.
- **Empty source files are common** — e.g. `04-Knowledge/claude/api.md` was empty. Always check file size/content before delegating a write based on it.
- **`05-Brain-Dump/` is often empty** — don't assume it has content. When the user says "find X in my brain dump", also check `10-Wiki/hot.md` (recent activity cache), `02-Projects/`, and run a broad `grep -ri` across the whole vault before giving up.
- **Vault search sequence for "find a project"**: (1) check `10-Wiki/hot.md` for recent activity, (2) check `02-Projects/`, (3) grep the whole vault for keywords, (4) only then report not found.
- **Docker mount is `/workspace/vault/`** — NOT `/workspace/vault/`. This is the correct path inside the container.
- **Dataview blocks inside code blocks** — if nesting a prompt template inside a dashboard, use a plain code block (not dataview) to prevent it from being interpreted as a query.

## Obsidian Mobile Sync (Android)

The vault lives at `/home/prox/Documents/ProxVault` on the host. Two recommended sync methods:

### Syncthing (recommended — local, private, free)
- Install: `sudo apt install syncthing` on host
- Enable: `systemctl --user enable --now syncthing`
- Web UI: http://127.0.0.1:8384
- Share folder: `/home/prox/Documents/ProxVault`
- Android: install Syncthing app + Obsidian, point vault at synced folder

### Remotely Save plugin (easiest — uses existing cloud)
- Install "Remotely Save" Obsidian community plugin
- Configure with Google Drive, OneDrive, Dropbox, or S3
- Install same plugin on Android Obsidian, connect same account

See `references/obsidian-android-sync.md` for full setup steps.

## Verification
- After creating a wiki page, read it back to confirm it saved correctly
- After searching, report which files contained matches and summarize the findings
- If a write fails, it means the target folder is read-only — redirect to `10-Wiki/`
