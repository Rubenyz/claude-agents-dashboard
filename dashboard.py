#!/usr/bin/env python3
"""Claude Agents Dashboard.

Small native tray/dashboard window that shows all active Claude Code agents:
each agent gets a stable session color, its name, your last message, a recap
(the first line of Claude's latest answer), the status (busy / waiting for
input) and the working directory. Click an agent to raise its Konsole window.

Below the active agents there are two "continue where you left off" sections:
- 📌 Pick up: open documents from ~/.claude/pickups (registered with the
  pickup skill), until they are checked off via the right-click menu.
- Recent sessions (Today / Yesterday / ...): sessions that are no longer
  running, read from the transcripts. Clicking one opens a new Konsole in the
  project directory with `claude --resume <sessionId>`, even after a reboot.

Data sources (all under ~/.claude, or $CLAUDE_CONFIG_DIR when set):
- sessions/<pid>.json   -> live registry per active session
- history.jsonl         -> your last typed message per session
- projects/*/<id>.jsonl -> recap, title, cwd (also of dead sessions)
- pickups/*.json        -> open "document this" items

Runs as a background process in the system tray. Closing hides to the tray.
"""
import colorsys
import datetime
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from PyQt5 import QtCore, QtGui, QtNetwork, QtWidgets

APP_ID = "claude-agents-dashboard"
HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "icon.svg")
CONFIG_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.expanduser("~/.claude"))
SESSIONS_DIR = os.path.join(CONFIG_DIR, "sessions")
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.jsonl")
PROJECTS_DIR = os.path.join(CONFIG_DIR, "projects")
PICKUPS_DIR = os.path.join(CONFIG_DIR, "pickups")
TITLE_CACHE_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
RUN_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
POLL_MS = 2000
WIDTH = 1000
RECENT_DAYS = 7    # how far back the "continue" list looks
RECENT_MAX = 12    # maximum number of recent sessions in the list

# Status -> (label, text color, background color) for the status pill.
STATUS_STYLE = {
    "busy":  ("busy", "#137333", "#e6f4ea"),
    "shell": ("busy", "#137333", "#e6f4ea"),
    "idle":  ("waiting for input", "#b06000", "#fdf0d5"),
}
STATUS_OTHER = ("#5f6368", "#eef0f2")

QDBUS = next((p for p in ("/usr/bin/qdbus6", "/usr/bin/qdbus", "/usr/bin/qdbus-qt6")
             if os.path.exists(p)), "qdbus6")


def pid_alive(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
            return b"claude" in fh.read()
    except (OSError, ValueError, TypeError):
        return False


def load_agents():
    agents = []
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if pid and pid_alive(pid):
            agents.append(data)
    agents.sort(key=lambda d: d.get("startedAt", 0))
    return agents


def display_name(data):
    name = (data.get("name") or "").strip()
    if name:
        return name
    sid = data.get("sessionId", "")
    cache = os.path.join(TITLE_CACHE_DIR, "claude-konsole-title-%s" % sid)
    try:
        with open(cache, "r") as fh:
            val = fh.read().strip()
        if val and val != "%d : %n":
            return val
    except OSError:
        pass
    cwd = data.get("cwd", "")
    return os.path.basename(cwd) or (sid[:8] if sid else "claude")


def session_color(sid):
    """Stable, recognizable color per session, derived from the sessionId."""
    h = int(hashlib.md5((sid or "").encode()).hexdigest(), 16)
    hue = (h % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.52, 0.58)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def short_cwd(cwd):
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    return cwd


def _clean_msg(d):
    disp = (d.get("display") or "").strip()
    if (not disp) or disp.startswith("[Pasted text"):
        pc = d.get("pastedContents") or {}
        for k in sorted(pc):
            c = pc[k]
            if isinstance(c, dict) and c.get("content"):
                disp = c["content"]
                break
    return " ".join(disp.split()).lstrip("❯").strip()


_msg_cache = {"mtime": None, "data": {}}


def load_last_messages():
    """sessionId -> your most recent typed message, cached on mtime."""
    try:
        mt = os.path.getmtime(HISTORY_PATH)
    except OSError:
        return {}
    if _msg_cache["mtime"] == mt:
        return _msg_cache["data"]
    best = {}
    try:
        with open(HISTORY_PATH, "r") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                sid = d.get("sessionId")
                ts = d.get("timestamp", 0)
                if not sid or (sid in best and best[sid][0] >= ts):
                    continue
                text = _clean_msg(d)
                if text:
                    best[sid] = (ts, text)
    except OSError:
        return _msg_cache["data"]
    _msg_cache["mtime"] = mt
    _msg_cache["data"] = {sid: v[1] for sid, v in best.items()}
    return _msg_cache["data"]


_recap_cache = {}  # sid -> (mtime, first line)


def last_recap(sid):
    """Recap: the first meaningful line of Claude's latest answer."""
    if not sid:
        return ""
    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "%s.jsonl" % sid))
    if not paths:
        return ""
    path = paths[0]
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return _recap_cache.get(sid, (None, ""))[1]
    cached = _recap_cache.get(sid)
    if cached and cached[0] == mt:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 131072))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return cached[1] if cached else ""
    text = ""
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        content = d.get("message", {}).get("content")
        parts = []
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
        elif isinstance(content, str):
            parts.append(content)
        joined = "\n".join(parts).strip()
        if joined:
            text = joined
    first = ""
    for ln in text.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln:
            first = ln
            break
    _recap_cache[sid] = (mt, first)
    return first


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_summary_cache = {}  # path -> (mtime, info)


def _scan_transcript(chunk, info):
    """Fill info with title, cwd, last prompt and last answer found in chunk."""
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "ai-title" and d.get("aiTitle"):
            info["title"] = d["aiTitle"]
        elif t == "agent-name" and d.get("agentName"):
            info["title"] = d["agentName"]
        elif t == "assistant":
            content = d.get("message", {}).get("content")
            parts = []
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        parts.append(b["text"])
            elif isinstance(content, str):
                parts.append(content)
            joined = "\n".join(parts).strip()
            if joined:
                info["_last_ass"] = joined
        elif t == "user" and not d.get("isMeta"):
            content = d.get("message", {}).get("content")
            if (isinstance(content, str) and content.strip()
                    and not content.lstrip().startswith("<")):
                info["last_user"] = " ".join(content.split())[:220]
        if not info["cwd"] and isinstance(d.get("cwd"), str):
            info["cwd"] = d["cwd"]


def session_summary(path, sid):
    """Title, cwd, last prompt and recap of a session transcript (mtime cache)."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    cached = _summary_cache.get(path)
    if cached and cached[0] == mt:
        return cached[1]
    info = {"sid": sid, "mtime": mt, "title": "", "cwd": "",
            "last_user": "", "recap": "", "_last_ass": ""}
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    _scan_transcript(chunk, info)
    if (not info["title"] or not info["cwd"]) and size > 262144:
        # The title and cwd sometimes only appear early in a long transcript.
        head = {"title": "", "cwd": "", "last_user": "", "_last_ass": ""}
        try:
            with open(path, "rb") as fh:
                _scan_transcript(fh.read(131072).decode("utf-8", "ignore"), head)
        except OSError:
            pass
        info["title"] = info["title"] or head["title"]
        info["cwd"] = info["cwd"] or head["cwd"]
    for ln in info.pop("_last_ass").splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln:
            info["recap"] = ln
            break
    _summary_cache[path] = (mt, info)
    return info


def load_recent_sessions(active_sids):
    """Recent, no longer active sessions from the transcripts ('continue')."""
    cutoff = time.time() - RECENT_DAYS * 86400
    cands = []
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        sid = os.path.basename(path)[:-6]
        if not UUID_RE.match(sid) or sid in active_sids:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size == 0 or st.st_mtime < cutoff:
            continue
        cands.append((st.st_mtime, path, sid))
    cands.sort(reverse=True)
    out = []
    for mt, path, sid in cands:
        info = session_summary(path, sid)
        if not info or (not info["title"] and not info["last_user"]):
            continue  # empty or warmup session
        out.append(info)
        if len(out) >= RECENT_MAX:
            break
    return out


def load_open_docs():
    """Open 'document this' items from ~/.claude/pickups, newest first."""
    docs = []
    for path in sorted(glob.glob(os.path.join(PICKUPS_DIR, "*.json")), reverse=True):
        try:
            with open(path, "r") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if d.get("status") != "open":
            continue
        d["_path"] = path
        docs.append(d)
    return docs


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def day_label(ts):
    d = datetime.date.fromtimestamp(ts)
    today = datetime.date.today()
    if d == today:
        return "Today"
    if d == today - datetime.timedelta(days=1):
        return "Yesterday"
    return "%s %d %s" % (DAYS[d.weekday()], d.day, MONTHS[d.month - 1])


def claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def terminal_argv(cwd, cmd):
    """Command line for the first available terminal emulator, running cmd.

    $TERMINAL wins when set; otherwise Konsole first (KDE is this tool's home
    turf), then other common emulators. Callers should also pass cwd to Popen
    for emulators that only inherit their working directory.
    """
    builders = {
        "konsole": ["konsole", "--workdir", cwd, "-e"],
        "gnome-terminal": ["gnome-terminal", "--working-directory=" + cwd, "--"],
        "alacritty": ["alacritty", "--working-directory", cwd, "-e"],
        "kitty": ["kitty", "--directory", cwd],
        "foot": ["foot", "--working-directory=" + cwd],
        "wezterm": ["wezterm", "start", "--cwd", cwd, "--"],
        "x-terminal-emulator": ["x-terminal-emulator", "-e"],
        "xterm": ["xterm", "-e"],
    }
    order = ["konsole", "gnome-terminal", "alacritty", "kitty", "foot",
             "wezterm", "x-terminal-emulator", "xterm"]
    term = os.path.basename(os.environ.get("TERMINAL") or "")
    if term in builders:
        order.remove(term)
        order.insert(0, term)
    elif term and shutil.which(term):
        return [term, "-e"] + cmd  # unknown $TERMINAL: assume xterm-style -e
    for name in order:
        if shutil.which(name):
            return builders[name] + cmd
    return None


def copy_resume_cmd(cwd, sid):
    QtWidgets.QApplication.clipboard().setText(
        "cd %s && claude --resume %s" % (shlex.quote(cwd or "."), sid))


def open_path(path):
    if path:
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
_settings_cache = {"mtime": None, "effort": None}


def _global_effort():
    """Globally configured effort level from settings.json (per-session fallback)."""
    try:
        mt = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        return None
    if _settings_cache["mtime"] != mt:
        try:
            with open(SETTINGS_PATH) as fh:
                _settings_cache["effort"] = json.load(fh).get("effortLevel")
        except (OSError, ValueError):
            _settings_cache["effort"] = None
        _settings_cache["mtime"] = mt
    return _settings_cache["effort"]


def pretty_model(m):
    """'claude-opus-4-8' -> 'Opus 4.8', 'claude-haiku-4-5-2025...' -> 'Haiku 4.5'."""
    if not m:
        return ""
    s = m.split("[")[0]
    if s.startswith("claude-"):
        s = s[len("claude-"):]
    parts = s.split("-")
    if not parts:
        return m
    fam = parts[0].capitalize()
    nums = [p for p in parts[1:] if p.isdigit()]
    if len(nums) >= 2:
        return "%s %s.%s" % (fam, nums[0], nums[1])
    if nums:
        return "%s %s" % (fam, nums[0])
    return fam


MODE_NAMES = {
    "default": "default",
    "plan": "plan",
    "acceptEdits": "accept edits",
    "bypassPermissions": "bypass",
    "auto": "auto",
}

_EFFORT_RE = re.compile(
    r"<command-name>/effort</command-name>.*?<command-args>([^<]*)</command-args>",
    re.DOTALL)


def _context_pct(usage):
    """Estimated percentage of the context window that is in use."""
    if not isinstance(usage, dict):
        return None
    used = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))
    if used <= 0:
        return None
    # The model name does not reveal the 1M variant; above 200k tokens we
    # assume a 1M context window.
    window = 1000000 if used > 200000 else 200000
    return min(100, int(round(used * 100.0 / window)))


def _statusline_ctx(sid):
    """The exact context percentage a statusline script writes per session."""
    if not sid:
        return None
    path = os.path.join(RUN_DIR, "claude-agents-ctx-%s.json" % sid)
    try:
        with open(path) as fh:
            pct = json.load(fh).get("pct")
    except (OSError, ValueError):
        return None
    if pct is None:
        return None
    try:
        return min(100, int(round(float(pct))))
    except (TypeError, ValueError):
        return None


_meta_cache = {}  # sid -> (mtime, dict)


def session_meta(sid):
    """Model / effort / mode / context-% from this session's transcript."""
    meta = {"model": "", "effort": _global_effort(), "mode": "", "ctx": None}
    if not sid:
        return meta
    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "%s.jsonl" % sid))
    if not paths:
        return meta
    path = paths[0]
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return _meta_cache.get(sid, (None, meta))[1]
    cached = _meta_cache.get(sid)
    if cached and cached[0] == mt:
        meta = cached[1]
        real = _statusline_ctx(sid)
        if real is not None:
            meta["ctx"] = real
        return meta
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 131072))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return cached[1] if cached else meta
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "assistant":
            msg = d.get("message", {})
            if msg.get("model"):
                meta["model"] = msg["model"]
            if msg.get("usage"):
                pct = _context_pct(msg["usage"])
                if pct is not None:
                    meta["ctx"] = pct
        elif t == "permission-mode":
            meta["mode"] = d.get("permissionMode") or meta["mode"]
        elif t == "user":
            content = d.get("message", {}).get("content")
            if isinstance(content, str) and "/effort" in content:
                m = _EFFORT_RE.search(content)
                if m and m.group(1).strip():
                    meta["effort"] = m.group(1).strip()
    _meta_cache[sid] = (mt, meta)
    real = _statusline_ctx(sid)
    if real is not None:
        meta["ctx"] = real
    return meta


def konsole_pid_for(claude_pid):
    """Walk up the process hierarchy until the hosting konsole is found."""
    p = int(claude_pid)
    for _ in range(24):
        try:
            comm = open("/proc/%d/comm" % p).read().strip()
        except OSError:
            return None
        if comm == "konsole":
            return p
        try:
            stat = open("/proc/%d/stat" % p).read()
            ppid = int(stat[stat.rindex(")") + 1:].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if ppid <= 1:
            return None
        p = ppid
    return None


class ElidedLabel(QtWidgets.QLabel):
    """Label that elides its text with '…' and never claims more width than available."""

    def __init__(self, text, color, px, bold=False, italic=False):
        super().__init__(text)
        self._color = QtGui.QColor(color)
        f = self.font()
        f.setPixelSize(px)
        f.setBold(bold)
        f.setItalic(italic)
        self.setFont(f)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(self.fontMetrics().height() + 2)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setPen(self._color)
        text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.ElideRight, self.width())
        painter.drawText(self.rect(), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, text)


class WrapLabel(QtWidgets.QLabel):
    """Label that wraps its text over multiple lines; height follows the width."""

    def __init__(self, text, color, px, italic=False):
        super().__init__(text)
        f = self.font()
        f.setPixelSize(px)
        f.setItalic(italic)
        self.setFont(f)
        self.setWordWrap(True)
        self.setStyleSheet("color: %s;" % color)
        self.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        # Enable heightForWidth so the layout actually queries the wrapped
        # height at the current width (otherwise it measures a single column).
        sp = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                   QtWidgets.QSizePolicy.Minimum)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)


class AgentRow(QtWidgets.QWidget):
    clicked = QtCore.pyqtSignal(object)

    def __init__(self, data, last_msg, recap, meta=None):
        super().__init__()
        self.data = data
        meta = meta or {}
        self.setObjectName("AgentRow")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setCursor(QtCore.Qt.PointingHandCursor)

        tip = [display_name(data)]
        if last_msg:
            tip.append("\n\nYour last message:\n" + last_msg)
        if recap:
            tip.append("\n\nRecap:\n" + recap)
        tip.append("\n\n(click to raise the window)")
        self.setToolTip("".join(tip))
        self.setStyleSheet(
            "#AgentRow { background: #ffffff; }"
            " #AgentRow:hover { background: #f3f6f9; }")

        status = data.get("status", "")
        label, fg, bg = STATUS_STYLE.get(status, (status or "?",) + STATUS_OTHER)

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Session color bar on the left.
        bar = QtWidgets.QFrame()
        bar.setFixedWidth(5)
        bar.setStyleSheet("background: %s;" % session_color(data.get("sessionId", "")))
        bar.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(bar)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(12, 9, 12, 10)
        body.setSpacing(3)

        # Line 1: name + status pill.
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        name = ElidedLabel(display_name(data), "#1f2328", 15, bold=True)
        pill = QtWidgets.QLabel(label)
        pill.setStyleSheet(
            "color: %s; background: %s; font-size: 12px; font-weight: 600;"
            " padding: 1px 8px; border-radius: 8px;" % (fg, bg))
        pill.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(name, 1)
        top.addWidget(pill, 0)
        body.addLayout(top)

        # Line 2: your last message (wraps over multiple lines).
        if last_msg:
            body.addWidget(WrapLabel("❯ " + last_msg, "#4a5159", 13))

        # Line 3: recap (Claude's latest answer).
        if recap:
            body.addWidget(WrapLabel("↳ " + recap, "#6e7781", 13, italic=True))

        # Line 4: working directory.
        body.addWidget(ElidedLabel(short_cwd(data.get("cwd", "")), "#8c959f", 12))

        # Line 5: model · effort · mode · context-%.
        bits = []
        if meta.get("model"):
            bits.append(pretty_model(meta["model"]))
        if meta.get("effort"):
            bits.append("effort %s" % meta["effort"])
        if meta.get("mode"):
            bits.append(MODE_NAMES.get(meta["mode"], meta["mode"]))
        if meta.get("ctx") is not None:
            bits.append("context %d%%" % meta["ctx"])
        if bits:
            body.addWidget(ElidedLabel("  ·  ".join(bits), "#aab2bd", 12))

        self._body = body
        root.addLayout(body, 1)

    def height_for_width(self, w):
        """Required row height at a given width (follows the text wrapping)."""
        self.setFixedWidth(max(1, w))
        lay = self.layout()
        lay.activate()
        h = lay.heightForWidth(w)
        return h if h > 0 else self.sizeHint().height()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit(self.data)
        super().mouseReleaseEvent(event)


class SectionRow(QtWidgets.QWidget):
    """Non-clickable section header between the rows ('📌 Pick up', 'Today', ...)."""

    def __init__(self, text, accent=False):
        super().__init__()
        self.setObjectName("SectionRow")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "#SectionRow { background: %s; }" % ("#fdf6e3" if accent else "#f6f8fa"))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 5, 14, 4)
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet("color: %s; font-size: 12px; font-weight: 700;"
                          % ("#8a6d1a" if accent else "#57606a"))
        lay.addWidget(lbl)

    def height_for_width(self, w):
        return self.sizeHint().height()


class ResumeRow(QtWidgets.QWidget):
    """Row for a resumable session or an open 'pick up' document."""

    clicked = QtCore.pyqtSignal()

    def __init__(self, color, title, when="", lines=(), footers=(),
                 tooltip="", menu=()):
        super().__init__()
        self._menu = list(menu)
        self.setObjectName("AgentRow")  # same base/hover style as agent rows
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        self.setStyleSheet(
            "#AgentRow { background: #ffffff; }"
            " #AgentRow:hover { background: #f3f6f9; }")

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QtWidgets.QFrame()
        bar.setFixedWidth(5)
        bar.setStyleSheet("background: %s;" % color)
        bar.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(bar)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(12, 9, 12, 10)
        body.setSpacing(3)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(ElidedLabel(title, "#1f2328", 15, bold=True), 1)
        if when:
            wl = QtWidgets.QLabel(when)
            wl.setStyleSheet("color: #8c959f; font-size: 12px;")
            wl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            top.addWidget(wl, 0)
        body.addLayout(top)

        for text, col, italic in lines:
            body.addWidget(WrapLabel(text, col, 13, italic=italic))
        for text in footers:
            body.addWidget(ElidedLabel(text, "#8c959f", 12))
        root.addLayout(body, 1)

    def height_for_width(self, w):
        """Required row height at a given width (follows the text wrapping)."""
        self.setFixedWidth(max(1, w))
        lay = self.layout()
        lay.activate()
        h = lay.heightForWidth(w)
        return h if h > 0 else self.sizeHint().height()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if not self._menu:
            return
        menu = QtWidgets.QMenu(self)
        for label, fn in self._menu:
            menu.addAction(label).triggered.connect(fn)
        menu.exec_(event.globalPos())


class AgentList(QtWidgets.QListWidget):
    """Keeps the row width equal to the viewport so text elides instead of scrolling."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout()

    def relayout(self):
        """Recompute every row height at the current viewport width."""
        w = self.viewport().width()
        if w <= 1:
            return
        for i in range(self.count()):
            it = self.item(i)
            row = self.itemWidget(it)
            h = row.height_for_width(w) if row else it.sizeHint().height()
            it.setSizeHint(QtCore.QSize(w, h))


class Dashboard(QtWidgets.QWidget):
    def __init__(self, icon):
        super().__init__()
        self.setWindowTitle("Claude agents")
        self.setWindowIcon(icon)
        self.resize(WIDTH, 640)
        self.setMinimumWidth(360)
        self.setStyleSheet("background: #ffffff;")
        self._raise_seq = 0
        self._sized = False

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = QtWidgets.QLabel()
        self.header.setStyleSheet(
            "background: #f6f8fa; color: #1f2328; font-weight: 600;"
            " font-size: 15px; padding: 12px 14px;"
            " border-bottom: 1px solid #d0d7de;")
        outer.addWidget(self.header)

        self.list = AgentList()
        self.list.setStyleSheet(
            "QListWidget { border: none; background: #ffffff; outline: 0; }"
            " QListWidget::item { border-bottom: 1px solid #eaecef; }"
            " QListWidget::item:selected { background: transparent; }")
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.list.setFocusPolicy(QtCore.Qt.NoFocus)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.list.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                QtWidgets.QSizePolicy.Expanding)
        outer.addWidget(self.list, 1)

        self.empty = QtWidgets.QLabel("No active or recent Claude sessions")
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setStyleSheet("color: #8c959f; font-size: 14px; padding: 24px;")
        outer.addWidget(self.empty)

        self.count = 0

    def refresh(self):
        if QtWidgets.QApplication.activePopupWidget() is not None:
            return self.count  # do not rebuild while a context menu is open
        agents = load_agents()
        last_msgs = load_last_messages()
        self.count = len(agents)
        self.header.setText("Active Claude agents — %d" % self.count)
        active_sids = {a.get("sessionId", "") for a in agents}
        docs = load_open_docs()
        recents = load_recent_sessions(active_sids)
        self.list.clear()
        for data in agents:
            sid = data.get("sessionId", "")
            row = AgentRow(data, last_msgs.get(sid), last_recap(sid), session_meta(sid))
            row.clicked.connect(self.raise_agent)
            self._add_row(row)
        if docs:
            self._add_row(SectionRow("📌  Pick up — %d" % len(docs), accent=True))
            for doc in docs:
                self._add_row(self._doc_row(doc))
        prev_day = None
        for info in recents:
            lbl = day_label(info["mtime"])
            if lbl != prev_day:
                self._add_row(SectionRow(lbl))
                prev_day = lbl
            self._add_row(self._recent_row(info, last_msgs))
        total = self.list.count()
        self.list.setVisible(total > 0)
        self.empty.setVisible(total == 0)
        self._fit()
        return self.count

    def _add_row(self, row):
        item = QtWidgets.QListWidgetItem(self.list)
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        w = self.list.viewport().width()
        item.setSizeHint(QtCore.QSize(w, row.height_for_width(w)))

    def _doc_row(self, doc):
        cwd = doc.get("cwd", "")
        sid = doc.get("sessionId", "")
        lines = []
        if doc.get("next"):
            lines.append(("→ " + doc["next"], "#4a5159", False))
        footers = [short_cwd(doc.get("doc", ""))]
        if cwd:
            footers.append(short_cwd(cwd))
        row = ResumeRow(
            "#d4a72c", "📌 " + (doc.get("title") or "untitled"),
            when=(doc.get("created") or "")[:10], lines=lines, footers=footers,
            tooltip="Click: resume the session in a new Konsole.\n"
                    "Right-click: mark as done or open the document.",
            menu=[("Mark as done", lambda: self.finish_doc(doc)),
                  ("Open document", lambda: open_path(doc.get("doc", ""))),
                  ("Copy resume command", lambda: copy_resume_cmd(cwd, sid))])
        row.clicked.connect(lambda: self.resume(cwd, sid))
        return row

    def _recent_row(self, info, last_msgs):
        sid, cwd = info["sid"], info["cwd"]
        lines = []
        last = last_msgs.get(sid) or info["last_user"]
        if last:
            lines.append(("❯ " + last, "#4a5159", False))
        if info["recap"]:
            lines.append(("↳ " + info["recap"], "#6e7781", True))
        title = info["title"] or os.path.basename(cwd or "") or sid[:8]
        row = ResumeRow(
            session_color(sid), title,
            when=time.strftime("%H:%M", time.localtime(info["mtime"])),
            lines=lines, footers=[short_cwd(cwd)],
            tooltip="Click: resume in a new Konsole (claude --resume).",
            menu=[("Copy resume command", lambda: copy_resume_cmd(cwd, sid))])
        row.clicked.connect(lambda: self.resume(cwd, sid))
        return row

    def resume(self, cwd, sid):
        """Open a new terminal in the project directory and resume the session."""
        if not (cwd and os.path.isdir(cwd)):
            cwd = os.path.expanduser("~")  # project directory may be gone
        cmd = [claude_bin()]
        if sid and glob.glob(os.path.join(PROJECTS_DIR, "*", "%s.jsonl" % sid)):
            cmd += ["--resume", sid]
        argv = terminal_argv(cwd, cmd)
        if argv is None:
            # No known terminal emulator: at least hand over the command.
            copy_resume_cmd(cwd, sid)
            return
        try:
            subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            return
        self.hide()

    def finish_doc(self, doc):
        """Check off a pickup item; the file is kept with status 'done'."""
        path = doc.get("_path")
        if not path:
            return
        d = {k: v for k, v in doc.items() if k != "_path"}
        d["status"] = "done"
        d["closedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            with open(path, "w") as fh:
                json.dump(d, fh, ensure_ascii=False, indent=2)
        except OSError:
            return
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._sized:
            # Only after showing does the list have a real width; measure then.
            QtCore.QTimer.singleShot(0, self._fit)

    def _fit(self):
        """Determine a fitting initial height once; after that the user may
        resize freely. The list gets a scrollbar once the content no longer fits."""
        if self._sized:
            return
        if self.list.viewport().width() <= 1:
            return  # not laid out yet; showEvent/refresh retries later
        self.list.relayout()
        total = sum(self.list.item(i).sizeHint().height()
                    for i in range(self.list.count())) + 2
        screen = QtWidgets.QApplication.primaryScreen()
        avail = screen.availableGeometry().height() - 80 if screen else 900
        header_h = self.header.sizeHint().height()
        h = min(total + header_h, max(320, avail))
        self.resize(self.width(), h)
        self._sized = True

    def raise_agent(self, data):
        kpid = konsole_pid_for(data.get("pid"))
        if not kpid:
            return
        self._raise_seq += 1
        name = "carais%d" % self._raise_seq
        js = ('var t=%d;var ws=workspace.windowList();for(var i=0;i<ws.length;i++)'
              '{var c=ws[i];if(c&&c.pid===t&&String(c.resourceClass).toLowerCase()'
              '.indexOf("konsole")!==-1){c.minimized=false;workspace.activeWindow=c;}}'
              % kpid)
        jsfile = os.path.join(RUN_DIR, "claude-agents-raise-%d.js" % self._raise_seq)
        try:
            with open(jsfile, "w") as fh:
                fh.write(js)
        except OSError:
            return
        cmd = ('%(q)s org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript "%(f)s" "%(n)s"'
               ' && %(q)s org.kde.KWin /Scripting org.kde.kwin.Scripting.start'
               ' && sleep 0.3'
               ' && %(q)s org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript "%(n)s"'
               ' ; rm -f "%(f)s"'
               % {"q": QDBUS, "f": jsfile, "n": name})
        try:
            subprocess.Popen(["sh", "-c", cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class TrayApp:
    def __init__(self, app):
        self.app = app
        self.icon = QtGui.QIcon(ICON_PATH)
        if self.icon.isNull():
            self.icon = QtGui.QIcon.fromTheme("utilities-system-monitor")

        self.win = Dashboard(self.icon)

        self.tray = QtWidgets.QSystemTrayIcon(self.icon)
        self.tray.setToolTip("Claude agents")
        menu = QtWidgets.QMenu()
        menu.addAction("Show dashboard").triggered.connect(self.show_window)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self.app.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(POLL_MS)
        self.tick()

    def tick(self):
        count = self.win.refresh()
        self.tray.setToolTip("Claude agents — %d active" % count)

    def on_tray_activated(self, reason):
        if reason in (QtWidgets.QSystemTrayIcon.Trigger,
                      QtWidgets.QSystemTrayIcon.MiddleClick):
            self.toggle_window()

    def toggle_window(self):
        if self.win.isVisible() and not self.win.isMinimized():
            self.win.hide()
        else:
            self.show_window()

    def show_window(self):
        self.win.refresh()
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()
        # After a possible first height adjustment (_fit), place bottom-right.
        QtCore.QTimer.singleShot(0, self._place_window)

    def _place_window(self):
        scr = self.app.primaryScreen().availableGeometry()
        self.win.move(scr.right() - self.win.width() - 12,
                      scr.bottom() - self.win.height() - 12)


def main():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)

    # Attach a color-emoji fallback to the application font; without it Qt
    # renders emoji as an empty box/monochrome outline. Set the actually
    # resolved family as primary (not the alias "Sans Serif": with a families
    # list Qt would otherwise mistakenly pick the emoji font as the main font).
    font = app.font()
    real = QtGui.QFontInfo(font).family()
    font.setFamilies([real, "Noto Color Emoji"])
    app.setFont(font)

    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(200):
        sock.write(b"show\n")
        sock.waitForBytesWritten(200)
        sock.disconnectFromServer()
        return 0
    QtNetwork.QLocalServer.removeServer(APP_ID)
    server = QtNetwork.QLocalServer()
    server.listen(APP_ID)

    tray = TrayApp(app)

    def on_new_connection():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(lambda: (conn.readAll(), tray.show_window()))

    server.newConnection.connect(on_new_connection)

    if "--show" in sys.argv:
        tray.show_window()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
