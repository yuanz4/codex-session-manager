from __future__ import annotations

import curses
import os
import subprocess
import sys
import time

from . import sessions as S
from . import summarizer as SUM
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


def _wrap_text(text: str, width: int) -> list[str]:
    """Word-wrap text to fit `width`, preserving paragraph breaks (blank lines).

    Wraps on spaces when possible; falls back to hard-breaking long words. Never
    truncates — all content is shown across multiple lines.
    """
    if width <= 0:
        return []
    out: list[str] = []
    for para in text.split("\n"):
        if not para:
            out.append("")
            continue
        words = para.split(" ")
        line = ""
        for word in words:
            # A single word longer than width is hard-broken across lines.
            while len(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if line:
                if len(line) + 1 + len(word) <= width:
                    line += " " + word
                else:
                    out.append(line)
                    line = word
            else:
                line = word
        if line:
            out.append(line)
    return out


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
    # ID uses 12 chars: enough of a UUIDv7 to distinguish sessions created
    # close together (8 chars collide when created within the same minute).
    fixed = [
        ("SEL", 1),
        ("S", 2),
        ("ID", 13),
        ("SMRY", 4),
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
        ("ID", 13),
        ("SMRY", 4),
        ("TITLE", title_w),
        ("MODEL", 14),
        ("CWD", cwd_w),
        ("AGE", 5),
        ("TOK", 8),
    ]


def _summary_char(sess) -> str:
    state = getattr(sess, "summary_state", "none")
    if state == "done":
        return "✓"
    if state == "in_progress":
        return "…"
    return " "


SMRY_ATTR = {"done": 2, "in_progress": 4, "none": 0}  # color pair (2 green, 4 yellow)



# Status groups, in display order. Sessions are sorted into these groups; a
# session moves between groups automatically as its status changes (the TUI
# re-groups on every refresh, so a running session lands under "running" and
# slips to "ready" once its turn completes).
GROUP_ORDER = [S.STATUS_RUNNING, S.STATUS_READY, S.STATUS_ERROR]


def _grouped_sessions(sessions: list) -> list:
    """Sort sessions into status groups, preserving recency within each group."""
    order = {st: i for i, st in enumerate(GROUP_ORDER)}
    return sorted(sessions, key=lambda s: (order.get(s.status, len(GROUP_ORDER)), -(s.updated_at or 0)))


def _build_display(sessions: list) -> list[dict]:
    """Return display rows: group headers + session rows.

    Each session row carries `idx` = its index in `sessions` (the grouped list),
    which the selection model uses. Group headers are not selectable.
    """
    rows: list[dict] = []
    for g in GROUP_ORDER:
        members = [(i, s) for i, s in enumerate(sessions) if s.status == g]
        if not members:
            continue
        rows.append({"kind": "group", "status": g, "count": len(members)})
        for i, s in members:
            rows.append({"kind": "session", "idx": i, "sess": s})
    # Any unrecognized status -> tail group so they still render.
    others = [(i, s) for i, s in enumerate(sessions) if s.status not in GROUP_ORDER]
    if others:
        rows.append({"kind": "group", "status": "other", "count": len(others)})
        for i, s in others:
            rows.append({"kind": "session", "idx": i, "sess": s})
    return rows


def _draw(stdscr, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    layout = _column_layout(w)
    now = time.strftime("%H:%M:%S")
    sessions = state["filtered"]

    # Header
    home = S.codex_home(state.get("home"))
    counts = {st: sum(1 for s in sessions if s.status == st) for st in GROUP_ORDER}
    parts = "  ".join(f"{S.ICON.get(st,'?')} {st}:{counts[st]}" for st in GROUP_ORDER if counts[st])
    title = f" codex-session-manager  {len(sessions)} session(s) ·{parts}   {home}   {now} "
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

    display = _build_display(sessions)
    # Map selected session index -> display row, to keep it scrolled into view.
    sel_disp = 0
    for di, r in enumerate(display):
        if r["kind"] == "session" and r["idx"] == state["selected"]:
            sel_disp = di
            break
    if state["offset"] > sel_disp:
        state["offset"] = sel_disp
    elif state["offset"] + visible <= sel_disp:
        state["offset"] = sel_disp - visible + 1
    if state["offset"] < 0:
        state["offset"] = 0

    INDENT = 2
    for i in range(visible):
        di = state["offset"] + i
        if di >= len(display):
            break
        row = display[di]
        row_y = body_top + i

        if row["kind"] == "group":
            st = row["status"]
            icon = S.ICON.get(st, "?")
            color = curses.color_pair(COLOR.get(st, 0)) | curses.A_BOLD
            label = st if st != "other" else "other"
            try:
                stdscr.addstr(row_y, 0, " " * w)  # clear row
                stdscr.addstr(row_y, 0, f"{icon} {label} ({row['count']})", color)
            except curses.error:
                pass
            continue

        sess = row["sess"]
        selected = row["idx"] == state["selected"]
        attr_bar = curses.color_pair(5) if selected else 0

        # full-width selection bar (indented)
        try:
            stdscr.addstr(row_y, 0, " " * w, attr_bar)
        except curses.error:
            pass

        cols = [
            "▶" if selected else " ",
            S.ICON.get(sess.status, "?"),
            sess.short_id,
            _summary_char(sess),
            sess.title or "(no title)",
            sess.model,
            sess.cwd,
            sess.age,
            f"{sess.tokens}",
        ]
        for (label, cw), cx, val in zip(layout, col_x, cols):
            gx = cx + INDENT
            if label == "SEL":
                if selected:
                    try:
                        stdscr.addstr(row_y, gx, "▶", curses.color_pair(COLOR.get(sess.status, 0)) | curses.A_BOLD)
                    except curses.error:
                        pass
                continue
            if label == "S":
                color = curses.color_pair(COLOR.get(sess.status, 0))
                if selected:
                    color = color | curses.A_BOLD
                try:
                    stdscr.addstr(row_y, gx, val.ljust(cw)[:cw], color)
                except curses.error:
                    pass
                continue
            if label == "SMRY":
                cp = SMRY_ATTR.get(getattr(sess, "summary_state", "none"), 0)
                color = curses.color_pair(cp) if cp else attr_bar
                if selected and cp:
                    color = color | curses.A_BOLD
                if getattr(sess, "summary_state", "none") == "disabled":
                    color = color | curses.A_DIM
                try:
                    stdscr.addstr(row_y, gx, val.ljust(cw)[:cw], color)
                except curses.error:
                    pass
                continue
            text = _truncate(str(val), cw)
            try:
                stdscr.addstr(row_y, gx, text.ljust(cw)[:cw], attr_bar)
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

    keys = "→/Enter attach · n new · space summary · d delete · x kill · ? help · q quit"
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
    "  n             start a new Codex session (popup for optional seed prompt)",
    "  r             refresh now   (auto-refreshes every 5s)",
    "  space         view the summary for the selected session (tap again to close)",
    "  d             delete selected   (popup confirm; codex delete --force)",
    "  x             kill the tmux session for the selected Codex session",
    "  ?             show this help",
    "  q  /  Esc     quit",
    "",
    "Status:   ● running   ○ ready   ✖ error",
    "Summary:  ✓ ready   … summarizing   (blank: running / none yet). Always on;",
    "",
    "Returning to this menu from a Codex session:",
    "  ←  (left)      inside a Codex session, exits to menu ONLY when the prompt",
    "                  is empty and the cursor is at the input start. Otherwise ←",
    "                  moves the cursor normally so text editing is unaffected.",
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


def _enter_session(name: str, stdscr) -> None:
    """Switch this tmux client into a codex tmux session, keeping the manager alive.

    The manager keeps running in the `codex-sm` tmux session in the background; the
    user returns by pressing ← in the codex session (see left_or_exit.sh). When
    there is no attached tmux client we leave the codex session detached and
    show a hint instead of risking a nested attach.
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
    txt = _popup_input(stdscr, "New session", "prompt (optional, Enter to start):")
    if txt is None:
        return
    if not T.available():
        curses.endwin()
        try:
            subprocess.run(["codex"] + ([txt] if txt else []))
        finally:
            stdscr.touchwin()
            stdscr.refresh()
        return
    cwd = state["home_cwd"]
    name = T.ensure_new_session(txt or None, cwd)
    _enter_session(name, stdscr)


def _safe_edit_draw(win, txt: str) -> None:
    """Redraw a 1-line edit window, clamping text to its width. Never raises.

    addstr past the last column of a window returns ERR in curses (and raises
    here), which previously crashed the whole manager on long prompts/filters.
    """
    try:
        _, ww = win.getmaxyx()
        shown = txt[: max(0, ww - 1)]
        win.erase()
        win.addstr(0, 0, shown)
        win.refresh()
    except curses.error:
        pass


def _popup(stdscr, title: str, body_lines: list[str], height: int, width: int):
    """Open a centered bordered popup window. Returns (win, inner_top, inner_left, inner_w).

    Caller draws into win and reads input from it. Call _close_popup(win) after.
    """
    h, w = stdscr.getmaxyx()
    height = min(height, h - 2)
    width = min(width, w - 2)
    top = max(1, (h - height) // 2)
    left = max(1, (w - width) // 2)
    win = curses.newwin(height, width, top, left)
    win.keypad(True)
    try:
        win.border()
        win.attron(curses.A_BOLD)
        win.addstr(0, 2, f" {title} ")
        win.attroff(curses.A_BOLD)
    except curses.error:
        pass
    y = 1
    for line in body_lines:
        for wl in _wrap_text(line, width - 4):
            try:
                win.addstr(y, 2, wl)
            except curses.error:
                pass
            y += 1
    win.refresh()
    return win, y, 2, width - 4


def _close_popup(win) -> None:
    try:
        win.erase()
        win.refresh()
    except curses.error:
        pass


def _popup_input(stdscr, title: str, label: str, prefill: str = "") -> str | None:
    """Draw a popup with a single text input. Return text on Enter, None on Esc."""
    h, w = stdscr.getmaxyx()
    width = min(max(60, len(label) + 30), w - 2)
    height = 5
    win, inner_top, _, inner_w = _popup(stdscr, title, [label], height, width)
    box = curses.newwin(1, max(10, inner_w), stdscr.getmaxyx()[0] // 2 + 1,
                        (stdscr.getmaxyx()[1] - width) // 2 + 2)
    box.keypad(True)
    curses.curs_set(1)
    txt = prefill
    _safe_edit_draw(box, txt)
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
        _safe_edit_draw(box, txt)
    curses.curs_set(0)
    _close_popup(win)
    return txt


def _popup_confirm(stdscr, title: str, lines: list[str]) -> bool:
    """Draw a popup and read y/n. Returns True on y/Y/Enter, False on n/Esc/q."""
    h, w = stdscr.getmaxyx()
    width = min(max(50, max((len(l) for l in lines), default=0) + 6), w - 2)
    height = min(len(lines) + 4, h - 2)
    win, _, _, _ = _popup(stdscr, title, lines, height, width)
    try:
        win.addstr(height - 2, 2, "[y] yes   [n] no", curses.A_DIM)
    except curses.error:
        pass
    win.refresh()
    while True:
        ch = win.getch()
        if ch in (ord("y"), ord("Y"), curses.KEY_ENTER, 10, 13):
            _close_popup(win)
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q")):
            _close_popup(win)
            return False


def _codex_passthrough(args: list[str]) -> int:
    return subprocess.run(["codex", *args], capture_output=True, text=True).returncode


def _maybe_auto_summarize(state, home):
    """Keep summaries up to date as turns complete.

    Session status (running/ready/error) and summary status are independent: a
    session moves to 'ready' the instant its turn completes; the summary
    proceeds on its own (blank -> ... -> ready). On each refresh:
      - clear summaries of sessions that are now running (a new turn started):
        deletes the .txt so the SMRY column goes blank, regenerates on completion;
      - (re)summarize sessions whose latest normally-completed turn differs from
        the one we last summarized. Interrupted/errored turns are skipped.
    """
    SUM.clear_running_summaries(state["sessions"], home)
    SUM.maybe_update(state["sessions"], home)


def _refresh_summary_states(sessions, home) -> None:
    """Re-read each session's summary state/summary from disk so the UI reflects
    changes the summarizer just made (clearing, starting, completing). Session
    status is NOT recomputed here — it stays as loaded (independent of summary).
    """
    for s in sessions:
        s.summary_state = SUM.summary_state(s.id, home)
        s.summary = SUM.read_summary(s.id, home) if s.summary_state == "done" else None


def _view_summary(state, stdscr) -> None:
    """Open the summary popup for the selected session; tap space (or Esc/q) to close."""
    if not state["filtered"]:
        return
    sess = state["filtered"][state["selected"]]
    # Read summary state from disk for accuracy (in-memory state may lag between
    # auto-refreshes; session status and summary status are independent).
    state_sum = SUM.summary_state(sess.id, state["home"])
    # If a summarizer is running, say so clearly instead of "no summary".
    if state_sum == "in_progress":
        h, w = stdscr.getmaxyx()
        win, _, _, _ = _popup(stdscr, f"Summary — {sess.short_id}", [
            "Still summarizing this session…",
            "",
            "The summary is being generated. Try again in a few seconds.",
            "",
            "press space/Esc to close",
        ], 7, min(56, w - 2))
        win.refresh()
        while True:
            ch = win.getch()
            if ch in (ord(" "), 27, ord("q")):
                break
        _close_popup(win)
        return
    text = sess.summary
    if not text:
        text = SUM.read_summary(sess.id, state["home"])
    if not text:
        # "No summary yet" — closes on space/Esc/q (consistent with summary popup).
        h, w = stdscr.getmaxyx()
        win, _, _, _ = _popup(stdscr, "Summary", [
            "No summary for this session yet.",
            "",
            "It will be summarized when its current turn completes normally.",
            "",
            "press space/Esc to close",
        ], 7, min(60, w - 2))
        win.refresh()
        while True:
            ch = win.getch()
            if ch in (ord(" "), 27, ord("q")):
                break
        _close_popup(win)
        return
    h, w = stdscr.getmaxyx()
    width = min(max(60, w - 8), w - 2)
    height = min(20, h - 2)
    win, _, _, inner_w = _popup(stdscr, f"Summary — {sess.short_id}  (space to close)", [], height, width)
    inner_w -= 2  # padding inside the border
    out_lines = _wrap_text(text, max(10, inner_w))
    # Scrollable view of out_lines.
    max_body = height - 3
    scroll = 0

    def _draw_lines():
        try:
            win.border()
            win.attron(curses.A_BOLD)
            win.addstr(0, 2, f" Summary — {sess.short_id}  (space to close) ")
            win.attroff(curses.A_BOLD)
        except curses.error:
            pass
        for i in range(max_body):
            idx = scroll + i
            try:
                if idx < len(out_lines):
                    ln = out_lines[idx]
                else:
                    ln = ""
                win.addstr(1 + i, 2, ln.ljust(inner_w))
            except curses.error:
                pass
        # scroll indicator
        if len(out_lines) > max_body:
            indicator = f" {scroll + 1}-{min(scroll + max_body, len(out_lines))}/{len(out_lines)} "
            try:
                win.addstr(height - 1, width - len(indicator) - 2, indicator, curses.A_DIM)
            except curses.error:
                pass
        win.refresh()

    _draw_lines()
    while True:
        ch = win.getch()
        if ch in (ord(" "), 27, ord("q")):
            break
        if ch in (curses.KEY_UP, ord("k")):
            scroll = max(0, scroll - 1)
            _draw_lines()
        elif ch in (curses.KEY_DOWN, ord("j")):
            scroll = min(max(0, len(out_lines) - max_body), scroll + 1)
            _draw_lines()
    _close_popup(win)


def _run(stdscr, home: str | None):
    _ensure_screen(stdscr)
    SUM.cleanup_stale_pending(home)  # orphaned .pending from a previous process
    state = {
        "home": home,
        "home_cwd": os.path.expanduser("~"),
        "sessions": S.load_sessions(home),
        "filtered": [],
        "selected": 0,
        "offset": 0,
        "last_refresh": 0.0,
    }
    state["filtered"] = _grouped_sessions(state["sessions"])
    # At startup, summarize (in parallel) every session that's missing a summary.
    SUM.summarize_all_missing(state["sessions"], home)
    curses.noecho()

    while True:
        now = time.time()
        if now - state["last_refresh"] >= REFRESH_SECS:
            state["sessions"] = S.load_sessions(home)
            _maybe_auto_summarize(state, home)
            _refresh_summary_states(state["sessions"], home)
            state["filtered"] = _grouped_sessions(state["sessions"])
            state["last_refresh"] = now
            if state["selected"] >= len(state["filtered"]):
                state["selected"] = max(0, len(state["filtered"]) - 1)
        _draw(stdscr, state)

        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (ord("q"), 27):
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
        elif ch == ord(" "):
            _view_summary(state, stdscr)
        elif ch == ord("d"):
            if state["filtered"]:
                sess = state["filtered"][state["selected"]]
                if _popup_confirm(stdscr, "Delete session", [
                    f"Permanently delete this session?",
                    "",
                    f"  id:    {sess.id}",
                    f"  title: {sess.title or '(none)'}",
                ]):
                    _codex_passthrough(["delete", "--force", sess.id])
                    SUM.reset_summary(sess.id, home)
                    state["last_refresh"] = 0.0
        elif ch == ord("x"):
            # kill the tmux session for the selected codex session
            if state["filtered"]:
                sess = state["filtered"][state["selected"]]
                T.kill_session(T.session_for(sess.id))
                state["last_refresh"] = 0.0


def run(home: str | None = None) -> int:
    if not T.in_tmux() and T.available():
        # Wrap ourselves in a tmux session so codex sessions can detach back here.
        _wrap_in_tmux()
        # If execvpe failed, fall through and run curses directly.
    try:
        return curses.wrapper(_run, home)
    except KeyboardInterrupt:
        return 0
