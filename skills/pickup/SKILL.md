---
name: pickup
description: Capture the current research/plan as a self-contained document and register it in the central pickup list (~/.claude/pickups), so the Claude Agents Dashboard reminds you about it under "📌 Pick up". Use for "document this", "save this for later", "I want to pick this up later". With the argument "done" you mark an open item as finished.
---

# Document for later

This system preserves research and plans so the user can pick them up days
later, even after a reboot. The Claude Agents Dashboard shows every open item
under "📌 Pick up"; clicking it resumes the session via `claude --resume`.

Choose the mode based on the arguments.

## Mode 1: capture (no arguments, or a topic/description)

1. Write a **self-contained** markdown document about the current research or
   plan. Write for two readers: the user a few days from now, and a fresh
   Claude session without any context. Required content:
   - context and motivation;
   - what was investigated or decided, with concrete file paths, commands
     and measurements;
   - open questions;
   - concrete next steps (numbered, actionable).

   Location: `docs/` in the project root if that directory exists, otherwise
   the project root itself. Filename: `pickup-<short-slug>.md`.

2. Gather metadata in a single Bash call:
   ```bash
   echo "$CLAUDE_CODE_SESSION_ID"; pwd; date -Is; date +%Y%m%d-%H%M
   ```

3. Register the document: write `~/.claude/pickups/<YYYYMMDD-HHMM>-<slug>.json`
   with exactly these fields:
   ```json
   {
     "title": "short title for the dashboard",
     "next": "one sentence: what should happen with this (e.g. 'execute the plan' or 'decide on X')",
     "doc": "/absolute/path/to/the/document.md",
     "cwd": "/absolute/path/of/the/project/directory",
     "sessionId": "<$CLAUDE_CODE_SESSION_ID>",
     "created": "<date -Is>",
     "status": "open"
   }
   ```
   Create `~/.claude/pickups/` if it does not exist yet.

4. Briefly report to the user: where the document lives, that it stays pinned
   in the dashboard under 📌 Pick up until checked off (right-click, "Mark as
   done"), and that this session can be resumed later with `claude --resume`
   (by clicking the item).

## Mode 2: finish (arguments contain "done")

1. Read all `~/.claude/pickups/*.json` with `"status": "open"`.
2. Determine which item is meant: the one belonging to this session/project,
   or the one named in the arguments. Show a short list and ask if it is
   ambiguous.
3. Set `"status": "done"` in that file and add `"closedAt"` (value of
   `date -Is`). Do not delete the file.
