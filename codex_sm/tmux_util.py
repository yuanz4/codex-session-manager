from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time

MANAGER_SESSION = "codex-sm"
RETURN_KEYTABLE = "codex_session"  # per-session no-prefix key table for codex sessions
RETURN_HINT = "← exits to menu when prompt is empty · Ctrl-b s to choose session"


def _left_or_exit_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "left_or_exit.sh")


def _setup_return_keytable() -> None:
    """Idempotently create the no-prefix key table used by codex sessions.

    In that table only `Left` is bound: it runs a helper that inspects the codex
    pane state and exits to the manager only when the prompt is empty and the
    cursor sits at the start of the input; otherwise it forwards the Left
    keystroke to Codex so editing behaves normally. Every other key passes
    through to the pane application. The manager session keeps the default root
    table, so this never affects the menu UI.
    """
    script = _left_or_exit_path()
    _run(["bind-key", "-T", RETURN_KEYTABLE, "Left", "run-shell",
          f"{script} " + "#{pane_id}"])


def _apply_return_keytable(session_name: str) -> None:
    """Point a session's no-prefix key table at the return handler."""
    _run(["set-option", "-t", session_name, "key-table", RETURN_KEYTABLE])



def available() -> bool:
    return shutil.which("tmux") is not None


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def session_exists(name: str) -> bool:
    if not available():
        return False
    return _run(["has-session", "-t", name]).returncode == 0


def list_sessions() -> list[str]:
    if not available():
        return []
    res = _run(["list-sessions", "-F", "#{session_name}"])
    if res.returncode != 0:
        return []
    return [ln for ln in res.stdout.splitlines() if ln]


def session_for(sess_id: str) -> str:
    """Convention: a codex session runs in tmux session `codex-<full id>`."""
    return f"codex-{sess_id}"


def ensure_resume_session(sess_id: str, cwd: str) -> str:
    """Create a detached tmux session running `codex resume <id>` if absent."""
    name = session_for(sess_id)
    if session_exists(name):
        return name
    cmd = f"codex resume {shlex.quote(sess_id)}"
    safe_cwd = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
    _run(
        ["new-session", "-d", "-s", name, "-c", safe_cwd, cmd],
        check=True,
    )
    _setup_return_keytable()
    _apply_return_keytable(name)
    # Give codex a moment to take over the pane.
    time.sleep(0.3)
    return name


def ensure_new_session(prompt: str | None, cwd: str) -> str:
    """Create a detached tmux session running a fresh `codex` (optionally seeded)."""
    name = f"codex-new-{int(time.time())}"
    if prompt:
        cmd = f"codex {shlex.quote(prompt)}"
    else:
        cmd = "codex"
    safe_cwd = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
    _run(["new-session", "-d", "-s", name, "-c", safe_cwd, cmd], check=True)
    _setup_return_keytable()
    _apply_return_keytable(name)
    return name


def display_message(msg: str, secs: float = 6.0) -> None:
    """Show a message in the tmux status bar (persists for `secs` seconds)."""
    _run(["display-message", "-d", str(int(secs * 1000)), msg])


def switch_to(name: str) -> bool:
    """Switch the current tmux client to another session (non-blocking)."""
    if not in_tmux():
        return False
    ok = _run(["switch-client", "-t", name]).returncode == 0
    if ok:
        display_message(f"[codex-sm] entered '{name}' — {RETURN_HINT}")
    return ok


def attach(name: str) -> None:
    """Attach (blocking) to a tmux session. Replaces the current process."""
    os.execvp("tmux", ["tmux", "attach", "-t", name])


def kill_session(name: str) -> None:
    if session_exists(name):
        _run(["kill-session", "-t", name])
