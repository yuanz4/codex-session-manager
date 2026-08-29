# codex-session-manager

An independent manager for [Codex](https://github.com/openai/codex) (Codex CLI) sessions.
It reads Codex's **native** session store — the on-disk `state_*.sqlite` threads
database and per-session `rollout-*.jsonl` event logs — and monitors each
session's runtime status:

| status   | symbol | meaning                                  |
|----------|:------:|------------------------------------------|
| running  | `●`    | a turn is in progress (`task_started` w/o `task_complete`) |
| ready    | `○`    | idle / finished (`task_complete`)        |
| error    | `✖`    | the last event was an error               |

Sessions are resumed with `codex resume <id>` inside **tmux**, so each session
lives in its own detachable tmux session (`codex-<id>`). Detach with `Ctrl-b d`
and you return to the manager.

## Requirements

- Python 3.10+ (stdlib only — no pip dependencies)
- `tmux` (for detachable attach; works without it, just less detachable)
- `codex` CLI on `PATH`

## Install

```bash
git clone <this-repo> ~/codex-session-manager
# option A: add to PATH
export PATH="$HOME/codex-session-manager:$PATH"
# option B: symlink
ln -s ~/codex-session-manager/codex-sm ~/.local/bin/codex-sm
```

## Usage

```bash
codex-sm            # interactive TUI (auto-wraps in a tmux session named codex-sm)
codex-sm list       # print sessions to stdout
codex-sm list --json
codex-sm status <id>      # status of one session (accepts short id prefix)
codex-sm attach <id>      # resume <id> in a tmux session and switch to it
codex-sm new [prompt]     # start a fresh Codex session in a tmux session
codex-sm delete <id>      # passthrough to `codex delete`
codex-sm --home /path     # override $CODEX_HOME (default ~/.codex)
```

### Interactive UI

Running `codex-sm` opens a curses list of all Codex sessions, auto-sorted by
recency and auto-refreshed every 5s.

| key            | action                                              |
|----------------|-----------------------------------------------------|
| `↑` `↓` / `j` `k` | move selection                                  |
| `g` / `G`      | jump to top / bottom                                |
| `Enter` / `→` | attach: resume that session in tmux and switch to it |
| `n`            | new session (optional seed prompt)                   |
| `r`            | refresh now (auto-refreshes every 5s)                |
| `/`            | filter by title / id / cwd                           |
| `D`            | delete selected (confirm) (`codex delete`)           |
| `x`            | kill the tmux session for the selected codex session |
| `?`            | show help / keybindings (in-app)                    |
| `q` / `Esc`    | quit (clears filter first, then quits)              |

**Arrow navigation:** `→` (right) on a session enters it; `←` (left) inside a
Codex session switches back to this menu. Left is **context-aware**: inside
Codex it exits only when the prompt is empty and the cursor is at the input
start; otherwise it moves the cursor normally so text editing works as usual
(detection uses the cursor column + Codex's dimmed placeholder).

### Detaching

The manager runs in tmux session `codex-sm`. Each attached Codex session runs
in tmux session `codex-<full-session-id>`. From a Codex session, pressing `←`
(on an empty prompt) returns to the manager; `Ctrl-b d` detaches the client
entirely (reattach with `tmux a -t codex-sm`).

> When `codex-sm` is launched outside tmux, it automatically re-executes itself
> inside a `codex-sm` tmux session so detaching works.

## How status is detected (native)

`codex_sm/sessions.py` reads:

- `~/.codex/state_*.sqlite`, table `threads` — the list of all sessions
  (`id`, `cwd`, `title`, `model`, `tokens_used`, `git_branch`,
  timestamps, `rollout_path`, ...). This is exactly what `codex resume` shows.
- the rollout JSONL at `rollout_path` — Codex appends `event_msg` entries with
  `task_started` / `task_complete` / `error` per turn. The most recent
  `task_started` turn without a matching `task_complete` ⇒ **running**; a
  terminal `error` ⇒ **error**; otherwise **ready**. This mirrors Codex's
  app-server `ThreadStatus` (`active` / `idle` / `systemError`).

This is read-only against the native store; no Codex internals are modified.

## Layout

```
codex-sm                  # launcher (sets PYTHONPATH, runs python -m codex_sm)
codex_sm/
  __init__.py
  __main__.py             # entry point
  cli.py                  # argparse subcommands
  sessions.py             # native codex store reader + status
  tmux_util.py            # tmux session create / resume / switch / attach
  tui.py                  # interactive curses UI
```
