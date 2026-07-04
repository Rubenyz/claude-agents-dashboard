# Claude Agents Dashboard

A small native (PyQt5) dashboard for [Claude Code](https://claude.com/claude-code)
that shows all **active Claude Code agents**: a stable color per session, its
name, your last message, a recap (the first line of Claude's latest answer),
the status (`busy` / `waiting for input`) and the working directory. Click a
row to **raise the corresponding Konsole window**.

Below the active agents there are two "continue where you left off" sections,
so you still know where you were after a reboot or days later:

- **📌 Pick up** — open documents registered with the bundled `pickup` skill
  in `~/.claude/pickups/*.json`. Clicking resumes the session it came from;
  right-click offers *Mark as done*, *Open document* and *Copy resume
  command*.
- **Recent sessions** (Today / Yesterday / ...) — sessions from the last 7
  days that are no longer running, across all your projects. Clicking opens a
  new terminal in the project directory with `claude --resume <sessionId>`
  (Konsole, `$TERMINAL` or another common emulator, whichever is available).

Runs as a background process in the **system tray** (KDE Plasma). Closing the
window hides it to the tray; quit via the tray menu.

## How it works

- **Active agents:** `~/.claude/sessions/<pid>.json` (the live registry Claude
  Code keeps per running session), filtered on pids that are actually alive.
  Much lighter than spawning `claude agents --json` every poll.
- **Your last message:** the last typed prompt per session from
  `~/.claude/history.jsonl`.
- **Session color:** derived deterministically from the `sessionId` (every
  session always gets the same color).
- **Raise window:** walk up from the claude pid to the hosting `konsole`
  process, then activate that window via KWin scripting
  (`workspace.activeWindow`).
- **Recent sessions:** the transcripts in `~/.claude/projects/*/<sessionId>.jsonl`
  survive reboots. The title (`ai-title` records), working directory (`cwd`
  field), last prompt and last answer are read from the head/tail of each file
  (cached on mtime); empty and warmup sessions are skipped.
- **Pickup items:** one JSON file per item in `~/.claude/pickups/`, with
  `title`, `next`, `doc`, `cwd`, `sessionId`, `created` and `status`
  (`open`/`done`). Checking off sets `status` to `done`; the file is kept.

## Install

```
./install.sh
```

This writes a launcher into `~/.local/share/applications` (pinnable in the
taskbar) and an autostart entry into `~/.config/autostart`, and (re)starts the
app from this directory.

### The pickup skill (optional but recommended)

```
cp -r skills/pickup ~/.claude/skills/
```

Then, in any Claude Code session, say "document this" (or `/pickup`) and
Claude writes a self-contained research/plan document into the project and
registers it in `~/.claude/pickups/`. The item stays pinned in the dashboard
under **📌 Pick up** until you check it off (right-click → *Mark as done*, or
`/pickup done`). Clicking the item brings you back into the exact session the
document came from, via `claude --resume`.

## Requirements

- Linux (Wayland or X11), `python3` with `PyQt5`
- Claude Code — the dashboard reads its session files under `~/.claude`, or
  `$CLAUDE_CONFIG_DIR` when that is set
- Best on KDE Plasma: raising the window of an *active* agent needs KWin,
  `qdbus6` and Konsole
- Resuming recent sessions works with any of: Konsole, GNOME Terminal,
  alacritty, kitty, foot, WezTerm, `x-terminal-emulator`, xterm, or whatever
  `$TERMINAL` points to

## Optional integrations

- **Exact context percentage:** if your Claude Code statusline script writes
  `$XDG_RUNTIME_DIR/claude-agents-ctx-<sessionId>.json` with
  `{"pct": <context_window.used_percentage>}`, the dashboard shows that exact
  number for active sessions; otherwise it estimates from the transcript.
- **Name fallback:** if a session has no name yet, the dashboard also looks at
  `$XDG_RUNTIME_DIR/claude-konsole-title-<sessionId>` (useful if you already
  mirror session names into your terminal title from a statusline hook).

## Files

- `dashboard.py` — the application
- `icon.svg` — tray/app icon
- `install.sh` — installs launcher + autostart and starts the app
- `skills/pickup/SKILL.md` — the "document this for later" skill for Claude Code

## Limitations

- Reads undocumented Claude Code internals: the live session registry,
  `history.jsonl` and transcript record types (written against Claude Code
  2.1.x). A future Claude Code release may change these formats.
- Raising the window of an *active* agent uses KWin scripting and is
  KDE-only; on other desktops that click is a no-op. Resuming recent
  sessions works on any desktop with a supported terminal; if none is found,
  the resume command is copied to the clipboard instead.
- The UI is a fixed light theme.
- On GNOME the system tray icon needs an AppIndicator extension.

## License

MIT
