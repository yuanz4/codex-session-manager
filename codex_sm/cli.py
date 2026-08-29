from __future__ import annotations

import argparse
import json
import sys

from . import sessions as S
from . import tmux_util as T
from . import tui


def _cmd_list(args) -> int:
    items = S.load_sessions(args.home)
    if args.json:
        out = []
        for s in items:
            out.append(
                {
                    "id": s.id,
                    "title": s.title,
                    "status": s.status,
                    "model": s.model,
                    "cwd": s.cwd,
                    "tokens": s.tokens,
                    "updated_at": s.updated_at,
                    "age": s.age,
                    "rollout_path": s.rollout_path,
                }
            )
        print(json.dumps(out, indent=2))
        return 0
    if not items:
        print("No Codex sessions found.")
        return 0
    rows = sorted(items, key=lambda s: s.updated_at or 0, reverse=True)
    cols = [(2, "S"), (8, "ID"), (38, "TITLE"), (14, "MODEL"), (18, "CWD"), (6, "AGE"), (8, "TOK")]

    def _field(s, name, w):
        if name == "S":
            return S.ICON.get(s.status, "?").ljust(w)
        val = {
            "ID": s.short_id,
            "TITLE": s.title or "(no title)",
            "MODEL": s.model,
            "CWD": s.cwd,
            "AGE": s.age,
            "TOK": str(s.tokens),
        }[name]
        if len(val) > w:
            val = val[: max(0, w - 1)] + "…"
        return val.ljust(w)

    header = "  ".join(name.ljust(w) for w, name in cols)
    print(header.rstrip())
    print("-" * min(len(header), 100))
    for s in rows:
        print("  ".join(_field(s, name, w) for w, name in cols))
    return 0


def _cmd_status(args) -> int:
    sess = S.find_by_id(args.id, args.home)
    if not sess:
        print(f"session not found: {args.id}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "id": sess.id,
                "status": sess.status,
                "title": sess.title,
                "model": sess.model,
                "cwd": sess.cwd,
                "tokens": sess.tokens,
                "updated_at": sess.updated_at,
                "tmux_session": T.session_for(sess.id) if T.session_exists(T.session_for(sess.id)) else None,
            },
            indent=2,
        )
    )
    return 0


def _cmd_attach(args) -> int:
    sess = S.find_by_id(args.id, args.home)
    if not sess:
        print(f"session not found: {args.id}", file=sys.stderr)
        return 1
    if not T.available():
        import subprocess

        return subprocess.run(["codex", "resume", sess.id]).returncode
    name = T.ensure_resume_session(sess.id, sess.cwd)
    if T.in_tmux():
        T.switch_to(name)
        return 0
    T.attach(name)
    return 0


def _cmd_new(args) -> int:
    import os

    cwd = os.path.expanduser("~")
    if not T.available():
        import subprocess

        return subprocess.run(["codex"] + ([args.prompt] if args.prompt else [])).returncode
    name = T.ensure_new_session(args.prompt, cwd)
    if T.in_tmux():
        T.switch_to(name)
        return 0
    T.attach(name)
    return 0


def _expand_id(short_id: str, home: str | None) -> str:
    sess = S.find_by_id(short_id, home)
    return sess.id if sess else short_id


def _cmd_delete(args) -> int:
    import subprocess

    return subprocess.run(["codex", "delete", _expand_id(args.id, args.home)]).returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-sm",
        description="Monitor and manage native Codex (codex) sessions, with tmux detachable attach.",
    )
    p.add_argument("--home", default=None, help="Override CODEX_HOME (default ~/.codex)")
    sub = p.add_subparsers(dest="cmd")

    p_tui = sub.add_parser("tui", help="Interactive curses UI (default)")
    p_tui.set_defaults(func=_cmd_tui)

    p_list = sub.add_parser("list", aliases=["ls"], help="List sessions to stdout")
    p_list.add_argument("--json", action="store_true", help="emit JSON")
    p_list.set_defaults(func=_cmd_list)

    p_status = sub.add_parser("status", help="Show status of one session")
    p_status.add_argument("id")
    p_status.set_defaults(func=_cmd_status)

    p_att = sub.add_parser("attach", help="Attach/resume a session in a tmux session")
    p_att.add_argument("id")
    p_att.set_defaults(func=_cmd_attach)

    p_new = sub.add_parser("new", help="Start a fresh Codex session in a tmux session")
    p_new.add_argument("prompt", nargs="?", default=None)
    p_new.set_defaults(func=_cmd_new)

    p_del = sub.add_parser("delete", help="Delete a session (passthrough to codex)")
    p_del.add_argument("id")
    p_del.set_defaults(func=_cmd_delete)

    return p


def _cmd_tui(args) -> int:
    return tui.run(home=args.home)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        # default to the interactive UI
        return tui.run(home=args.home)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
