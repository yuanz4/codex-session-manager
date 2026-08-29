from __future__ import annotations

import glob
import os
import sqlite3
import time
from dataclasses import dataclass

from . import summarizer as SUM

DEFAULT_CODEX_HOME = os.path.expanduser("~/.codex")

STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_ERROR = "error"

ICON = {
    STATUS_RUNNING: "●",
    STATUS_READY: "○",
    STATUS_ERROR: "✖",
}


def codex_home(override: str | None = None) -> str:
    return override or os.environ.get("CODEX_HOME") or DEFAULT_CODEX_HOME


@dataclass
class Session:
    id: str
    cwd: str
    title: str
    model: str
    source: str
    tokens: int
    created_at: int
    updated_at: int
    rollout_path: str | None
    git_branch: str | None
    preview: str
    name: str | None
    status: str = STATUS_READY
    summary: str | None = None
    summary_state: str = "none"  # done | in_progress | none

    @property
    def short_id(self) -> str:
        return self.id.split("-")[0] if self.id else ""

    @property
    def age(self) -> str:
        if not self.updated_at:
            return "?"
        secs = max(0, int(time.time()) - self.updated_at)
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"


def find_threads_db(home: str) -> str | None:
    for path in sorted(glob.glob(os.path.join(home, "state_*.sqlite")), reverse=True):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
            ).fetchone()
            con.close()
            if row:
                return path
        except sqlite3.Error:
            pass
    return None


def _row_value(row: sqlite3.Row, name: str, default=None):
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def load_sessions(home_override: str | None = None) -> list[Session]:
    home = codex_home(home_override)
    db = find_threads_db(home)
    if not db:
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT * FROM threads WHERE archived = 0"))
    con.close()

    sessions: list[Session] = []
    for row in rows:
        rollout_path = _row_value(row, "rollout_path")
        title = (
            _row_value(row, "name")
            or _row_value(row, "title")
            or _row_value(row, "first_user_message")
            or _row_value(row, "preview")
            or ""
        )
        # Hide summarizer sessions (transient; created+deleted by summarizer.py).
        for k in ("first_user_message", "preview", "title", "name"):
            v = _row_value(row, k)
            if v and v.startswith(SUM.SUMMARIZER_MARKER):
                title = None
                break
        if title is None:
            continue
        sess = Session(
            id=_row_value(row, "id", ""),
            cwd=_row_value(row, "cwd", "") or "",
            title=title,
            model=_row_value(row, "model", "") or "",
            source=_row_value(row, "source", "") or "",
            tokens=int(_row_value(row, "tokens_used", 0) or 0),
            created_at=int(_row_value(row, "created_at", 0) or 0),
            updated_at=int(_row_value(row, "updated_at", 0) or 0),
            rollout_path=rollout_path,
            git_branch=_row_value(row, "git_branch"),
            preview=_row_value(row, "preview", "") or "",
            name=_row_value(row, "name"),
        )
        sess.status = SUM.analyze_rollout(sess.rollout_path)["status"]
        sess.summary_state = SUM.summary_state(sess.id, home)
        if sess.summary_state == "done":
            sess.summary = SUM.read_summary(sess.id, home)
        sessions.append(sess)

    sessions.sort(key=lambda s: (s.updated_at or 0), reverse=True)
    return sessions


def find_by_id(sess_id: str, home_override: str | None = None) -> Session | None:
    sid = sess_id.strip().lower()
    for s in load_sessions(home_override):
        if s.id.lower() == sid or s.short_id.lower() == sid:
            return s
    return None
