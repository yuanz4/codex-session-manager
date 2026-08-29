from __future__ import annotations

import curses
import os
import shutil
import subprocess
import sys
import time

from . import sessions as S
from . import tmux_util as T

REFRESH_SECS = 5
COLOR = {
    S.STATUS_RUNNING: 1,  # cyan
    S.STATUS_READY: 2,   # green
    S.STATUS_ERROR: 3,    # red
}


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _wrap_in_tmux() -> None:
    """Re-exec the TUI inside a tmux session so codex sessions can detach back here."""
    if os.environ.get("CODEX_SM_NOWRAP"):
        return
    if T.session_exists(T.MANAGER_SESSION):
        if T.in_tmux():
            T.switch_to(T.MANAGER_SESSION)
        else:
            T.attach(T.MANAGER_SESSION)
        return
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else repo
    env["CODEX_SM_NOWRAP"] = "1"
    argv = ["tmux", "new-session", "-s", T.MANAGER_SESSION, sys.executable, "-m", "codex_sm", *sys.argv[1:]]
    try:
        os.execvpe(argv[0], argv, env)
    except OSError:
        pass


def _ensure_screen(stdscr):
    curses.curs_set(0)
    stdscr.timeout(1000)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selection bar
        curses.init_pair(6, curses.COLOR_BLUE, -1)  # header


def _column_layout(width: int) -> list[tuple[str, int]]:
    # (label, width). A leading SEL gutter renders the ▶ selection marker.
    fixed = [
        ("SEL", 1),
        ("S", 2),
        ("ID", 8),
        ("MODEL", 14),
        ("AGE", 5),
        ("TOK", 8),
    ]
    used = sum(w for _, w in fixed) + (len(fixed) - 1)  # spaces between cols
    remaining = max(10, width - used - 2)
    title_w = max(20, int(remaining * 0.55))
    cwd_w = max(15, remaining - title_w)
    return [
        ("SEL", 1),
        ("S", 2),
        ("ID", 8),
        ("TITLE", title_w),
        ("MODEL", 14),
        ("CWD", cwd_w),
        ("AGE", 5),
        ("TOK", 8),
    ]


def _draw(stdscr, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    layout = _column_layout(w)
    now = time.strftime("%H:%M:%S")
    sessions = state["filtered"]

    # Header
    home = S.codex_home(state.get("home"))
    title = f" codex-session-manager  {len(sessions)} session(s)   {home}   {now} "
    header_bar = title.ljust(w)[:w]
    try:
        stdscr.addstr(0, 0, header_bar, curses.color_pair(6) | curses.A_BOLD)
    except curses.error:
        pass

    # Column header
    x = 0
    col_x = []
    for label, cw in layout:
        col_x.append(x)
        hdr = "" if label == "SEL" else label
        try:
            stdscr.addstr(1, x, _truncate(hdr, cw), curses.A_BOLD)
        except curses.error:
            pass
        x += cw + 1
    try:
        stdscr.addstr(2, 0, "─" * w, curses.A_DIM)
    except curses.error:
        pass

    # Rows
    body_top = 3
    body_bottom = h - 3
    visible = body_bottom - body_top
    if visible < 1:
        visible = 1
    if state["selected"] < state["offset"]:
        state["offset"] = state["selected"]
    elif state["selected"] >= state["offset"] + visible:
        state["offset"] = state["selected"] - visible + 1

    for i in range(visible):
        idx = state["offset"] + i
        if idx >= len(sessions):
            break
        sess = sessions[idx]
        row_y = body_top + i
        selected = idx == state["selected"]
        attr_bar = curses.color_pair(5) if selected else 0

        # full-width selection bar
        try:
            stdscr.addstr(row_y, 0, " " * w, attr_bar)
        except curses.error:
            pass

        cols = [
            "▶" if selected else " ",
            S.ICON.get(sess.status, "?"),
            sess.short_id,
            sess.title or "(no title)",
            sess.model,
            sess.cwd,
            sess.age,
            f"{sess.tokens}",
        ]
        for (label, cw), cx, val in zip(layout, col_x, cols):
            if label == "SEL":
                if selected:
                    try:
                        stdscr.addstr(row_y, cx, "▶", curses.color_pair(COLOR.get(sess.status, 0)) | curses.A_BOLD)
                    except curses.error:
                        pass
                continue
            if label == "S":
                color = curses.color_pair(COLOR.get(sess.status, 0))
                if selected:
                    color = color | curses.A_BOLD
                try:
                    stdscr.addstr(row_y, cx, val.ljust(cw)[:cw], color)
                except curses.error:
                    pass
                continue
            text = _truncate(str(val), cw)
            try:
                stdscr.addstr(row_y, cx, text.ljust(cw)[:cw], attr_bar)
            except curses.error:
                pass

    # Status / footer
    if sessions:
        cur = sessions[state["selected"]]
        status_line = (
            f" [{S.ICON.get(cur.status,'?')}] {cur.status}  id={cur.id}  "
            f"cwd={cur.cwd}  "
            f"{'tmux:codex-'+cur.id if T.session_exists(T.session_for(cur.id)) else ''}"
        )
    else:
        status_line = " No sessions found in codex home."
    try:
        stdscr.addstr(h - 2, 0, _truncate(status_line, w), curses.A_DIM)
    except curses.error:
        pass

    keys = "→/Enter attach · n new · r refresh · /filter · D delete · x kill tmux · ? help · q quit"
    try:
        stdscr.addstr(h - 1, 0, _truncate(keys, w), curses.A_BOLD)
    except curses.error:
        pass
    stdscr.refresh()


HELP_LINES = [
    "codex-session-manager — keybindings",
    "",
    "  ↑ ↓  /  j k   move selection",
    "  g  /  G       top / bottom",
    "  Enter / →     resume selected session in a tmux session, switch into it",
    "  n             start a new Codex session (optional seed prompt)",
    "  r             refresh now   (auto-refreshes every 5s)",
    "  /             filter by title / id / cwd     (Esc clears)",
    "  D             delete selected   (codex delete, confirm)",
    "  x             kill the tmux session for the selected Codex session",
    "  ?             show this help",
    "  q  /  Esc     quit  (clears filter first)",
    "",
    "Status:  ● running   ○ ready   ✖ error",
    "",
    "Returning to this menu from a Codex session:",
    "  ←  (left)      inside a Codex session, exits to menu ONLY when the prompt",
    "                  is empty and the cursor is at the input start. Otherwise ←",
    "                  moves the cursor normally so text editing is unaffected.",
    "  Ctrl-b s       pick 'codex-sm' from tmux's session list (always available)",
    "",
    "Press any key to close this help.",
]


def _show_help(stdscr) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    try:
        stdscr.addstr(0, 0, HELP_LINES[0], curses.A_BOLD)
    except curses.error:
        pass
    for i, line in enumerate(HELP_LINES[1:], start=2):
        if i >= h - 1:
            break
        try:
            stdscr.addstr(i, 0, _truncate(line, w), curses.A_DIM if line.startswith("  ") else 0)
        except curses.error:
            pass
    stdscr.refresh()
    stdscr.nodelay(False)
    stdscr.getch()
    stdscr.timeout(1000)


def _filter(sessions, text):
    if not text:
        return list(sessions)
    t = text.lower()
    return [
        s for s in sessions
        if t in (s.title or "").lower() or t in (s.id or "").lower() or t in (s.cwd or "").lower()
    ]


def _wrap_resume(args) -> int:
    """Run codex resume directly (no tmux). Returns when codex exits."""
    res = subprocess.run(["codex", "resume", args["sess_id"]])
    return res.returncode


RETURN_HINT = "  (Ctrl-b s then pick 'codex-sm' to return to this menu, or Ctrl-b d to detach)"


def _enter_session(name: str, stdscr) -> None:
    """Switch this tmux client into a codex tmux session, keeping the manager alive.

    The manager keeps running in the `codex-sm` tmux session in the background, so
    the user can return via `Ctrl-b s` -> `codex-sm`. When there is no attached tmux
    client (e.g. the manager is running detached) we leave the codex session
    detached and show a hint instead of risking a nested attach.
    """
    if T.in_tmux():
        ok = T.switch_to(name)
        if ok:
            return
        # No attached client to switch — leave the codex session detached.
        _flash(stdscr, f"started '{name}' (detached). Attach with:  tmux attach -t {name}")
        return
    # Running outside tmux entirely (wrap failed): block on tmux attach, then resume.
    curses.endwin()
    subprocess.run(["tmux", "attach", "-t", name])
    stdscr.touchwin()
    stdscr.refresh()


def _flash(stdscr, msg: str, secs: float = 2.5) -> None:
    h, w = stdscr.getmaxyx()
    try:
        stdscr.addstr(h - 2, 0, " " * w)
        stdscr.addstr(h - 2, 0, _truncate(msg, w), curses.A_BOLD)
        stdscr.refresh()
    except curses.error:
        pass


def _attach(state, stdscr) -> None:
    sessions = state["filtered"]
    if not sessions:
        return
    sess = sessions[state["selected"]]
    if not T.available():
        curses.endwin()
        try:
            subprocess.run(["codex", "resume", sess.id])
        finally:
            stdscr.touchwin()
            stdscr.refresh()
        return
    name = T.ensure_resume_session(sess.id, sess.cwd)
    _enter_session(name, stdscr)


def _new_session(state, stdscr) -> None:
    if not T.available():
        curses.endwin()
        try:
            subprocess.run(["codex"] + (["env", "CODEX_SM_NEW=1"] if False else []))
        finally:
            stdscr.touchwin()
            stdscr.refresh()
        return
    # Prompt for an optional seed, very small input loop.
    curses.echo()
    curses.curs_set(1)
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 2, 0, " " * w)
    stdscr.addstr(h - 2, 0, " new session — prompt (optional): ")
    stdscr.refresh()
    box = curses.newwin(1, max(20, w - 50), h - 2, 49)
    box.keypad(True)
    txt = ""
    while True:
        ch = box.getch()
        if ch in (curses.KEY_ENTER, 10, 13):
            break
        if ch == 27:
            txt = None
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            txt = txt[:-1]
        elif 32 <= ch <= 126:
            txt += chr(ch)
        box.erase()
        box.addstr(0, 0, txt)
        box.refresh()
    curses.noecho()
    curses.curs_set(0)
    if txt is None:
        return
    cwd = state["home_cwd"]
    name = T.ensure_new_session(txt or None, cwd)
    _enter_session(name, stdscr)


def _codex_passthrough(args: list[str]) -> int:
    return subprocess.run(["codex", *args]).returncode


def _run(stdscr, home: str | None):
    _ensure_screen(stdscr)
    state = {
        "home": home,
        "home_cwd": os.path.expanduser("~"),
        "sessions": S.load_sessions(home),
        "filtered": [],
        "selected": 0,
        "offset": 0,
        "filter": "",
        "last_refresh": 0.0,
    }
    state["filtered"] = _filter(state["sessions"], "")
    curses.noecho()

    while True:
        now = time.time()
        if now - state["last_refresh"] >= REFRESH_SECS:
            state["sessions"] = S.load_sessions(home)
            state["filtered"] = _filter(state["sessions"], state["filter"])
            state["last_refresh"] = now
            if state["selected"] >= len(state["filtered"]):
                state["selected"] = max(0, len(state["filtered"]) - 1)
        _draw(stdscr, state)

        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (ord("q"), 27):
            if state["filter"]:
                state["filter"] = ""
                state["filtered"] = _filter(state["sessions"], "")
                state["selected"] = 0
                continue
            break
        if ch in (curses.KEY_UP, ord("k")):
            state["selected"] = max(0, state["selected"] - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            state["selected"] = min(len(state["filtered"]) - 1, state["selected"] + 1)
        elif ch in (curses.KEY_HOME, ord("g")):
            state["selected"] = 0
        elif ch in (curses.KEY_END, ord("G")):
            state["selected"] = max(0, len(state["filtered"]) - 1)
        elif ch == curses.KEY_RESIZE:
            curses.resizeterm(*stdscr.getmaxyx())
        elif ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
            _attach(state, stdscr)
        elif ch == ord("n"):
            _new_session(state, stdscr)
        elif ch == ord("?"):
            _show_help(stdscr)
        elif ch in (ord("r"), curses.KEY_REFRESH):
            state["last_refresh"] = 0.0
        elif ch == ord("/"):
            state["filter"] = _prompt_filter(stdscr)
            state["filtered"] = _filter(state["sessions"], state["filter"])
            state["selected"] = 0
        elif ch == ord("D"):
            if state["filtered"]:
                sess = state["filtered"][state["selected"]]
                if _confirm(stdscr, f"delete {sess.short_id}? y/n"):
                    _codex_passthrough(["delete", sess.id])
                    state["last_refresh"] = 0.0
        elif ch == ord("x"):
            # kill the tmux session for the selected codex session
            if state["filtered"]:
                sess = state["filtered"][state["selected"]]
                T.kill_session(T.session_for(sess.id))
                state["last_refresh"] = 0.0


def _prompt_filter(stdscr) -> str:
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 2, 0, " " * w)
    stdscr.addstr(h - 2, 0, " filter: ")
    stdscr.clrtoeol()
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    win = curses.newwin(1, max(20, w - 9), h - 2, 9)
    win.keypad(True)
    txt = ""
    while True:
        ch = win.getch()
        if ch in (curses.KEY_ENTER, 10, 13):
            break
        if ch == 27:
            txt = ""
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            txt = txt[:-1]
        elif 32 <= ch <= 126:
            txt += chr(ch)
        win.erase()
        win.addstr(0, 0, txt)
        win.refresh()
    curses.noecho()
    curses.curs_set(0)
    return txt


def _confirm(stdscr, msg: str) -> bool:
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 2, 0, " " * w)
    stdscr.addstr(h - 2, 0, f" {msg} ", curses.A_BOLD)
    stdscr.refresh()
    ch = stdscr.getch()
    return ch in (ord("y"), ord("Y"))


def run(home: str | None = None) -> int:
    if not T.in_tmux() and T.available():
        # Wrap ourselves in a tmux session so codex sessions can detach back here.
        _wrap_in_tmux()
        # If execvpe failed, fall through and run curses directly.
    try:
        return curses.wrapper(_run, home)
    except KeyboardInterrupt:
        return 0
