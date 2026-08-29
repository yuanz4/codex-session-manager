# codex-session-manager

Monitor and manage native [Codex](https://github.com/openai/codex) CLI sessions
in an interactive, tmux-detachable UI. Reads Codex's own session store — no
mocking, no scraping — and derives live status (running / ready / error) from
Codex's rollout event logs.

![codex-sm demo](assets/demo.gif)

![codex-sm UI](assets/ui.svg)

## Requirements

- Python 3.10+ (stdlib only — no pip dependencies)
- `tmux` (for detachable sessions)
- `codex` CLI on `PATH`

## Install

```bash
git clone <this-repo> ~/codex-session-manager
export PATH="$HOME/codex-session-manager:$PATH"   # or symlink codex-sm to ~/.local/bin
```

## Usage

```bash
codex-sm            # interactive UI (default)
codex-sm list       # one-shot table   (--json for JSON)
codex-sm status <id>      # one session (short id prefix OK)
codex-sm attach <id>      # resume <id> in tmux and switch to it
codex-sm new [prompt]     # start a fresh session in tmux
codex-sm delete <id>      # passthrough to `codex delete`
codex-sm --home /path     # override CODEX_HOME (default ~/.codex)
```

## Interactive UI

Sessions are **grouped by status**, auto-refreshed every 5s and auto-relocated
as status changes — a running session drops into `ready` when its turn
finishes, a failed one lands in `error`.

**Auto-summarizer (always on):** at startup, every session missing a summary is
summarized in parallel. During an ongoing conversation the summary is updated
each time a turn completes *normally* — so it reflects the latest state.
Interrupted or errored turns are not summarized (the summary stays at the last
normally-completed turn). A throwaway Codex session runs the summary under a
fixed prompt, then is deleted, leaving only a sidecar. The `SMRY` column shows
`✓` ready · `…` summarizing · blank none-yet. Press `space` to open a session's
summary; tap `space` (or `Esc`) again to close.

| key | action |
|-----|--------|
| `↑` `↓` / `j` `k` | move selection (skips group headers) |
| `g` / `G` | jump to top / bottom |
| `Enter` / `→` | enter the selected session (resume in tmux) |
| `n` | new session (popup for optional seed prompt) |
| `space` | open/close the selected session's summary |
| `d` | delete selected (popup confirm) |
| `x` | kill the tmux session for the selected codex session |
| `r` | refresh now |
| `?` | in-app help · `q`/`Esc` quit |

**In ↔ out:** `→` enters a session; inside a Codex session, `←` returns to the
menu — but only when the prompt is empty and the cursor sits at the input start,
so left-arrow cursor movement while editing still works normally.

Each session runs in its own tmux session (`codex-<id>`) behind the manager
(`codex-sm`), so detaching never stops your agents.

## How status is detected

`codex_sm/sessions.py` reads `~/.codex/state_*.sqlite` (`threads` table, the
same list `codex resume` uses) and each session's `rollout-*.jsonl`: the most
recent `task_started` turn without a matching `task_complete` ⇒ **running**, a
terminal `error` ⇒ **error**, else **ready** (mirrors Codex app-server
`ThreadStatus`). Read-only — no Codex internals are modified.

## Auto-summarizer

`codex_sm/summarizer.py` summarizes the latest *normally-completed* turn of
each session: at startup it parallelizes summaries for any session missing one,
and on each refresh it re-summarizes when a new turn completes normally (turn
status is read from the rollout's `task_started`/`task_complete`/`error`
events — turns that end via error or interrupt are skipped). It runs
`codex exec --json --skip-git-repo-check` with the turn's
`last_agent_message`, deletes the throwaway summarizer session with
`codex delete --force`, and stores the summary under
`~/.codex/sm_summaries/<id>.txt` (with `<id>.turn` tracking the last summarized
turn). Stale `.pending` markers from a killed manager are cleaned at startup.
Summarizer sessions are filtered from the UI.

## Layout

```
codex-sm                 # launcher
codex_sm/
  cli.py        sessions.py     tmux_util.py     tui.py     left_or_exit.sh
```
