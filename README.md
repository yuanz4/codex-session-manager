# codex-session-manager

Run many Codex agents in parallel without losing track. One dashboard shows
every session's status — running, ready, or errored — with summaries of what
each one did, so you can manage 10+ sessions at a glance.

![codex-session-manager](assets/session%20manager.png)

Watch the demo: [https://youtu.be/RQNtqrnr-eQ](https://youtu.be/RQNtqrnr-eQ)

## Getting started

```bash
git clone https://github.com/yuanz4/codex-session-manager.git ~/codex-session-manager
cd ~/codex-session-manager && ./codex-sm
```

That's it — the interactive UI opens. You'll see all your Codex sessions listed
and grouped by status.

## Using the interface

| key | what it does |
|-----|------|
| `↑` `↓` | move between sessions |
| `→` or `Enter` | enter the selected session to steer it |
| `←` | leave a session and return to the menu (only when the prompt is empty, so editing still works) |
| `n` | start a new session |
| `space` | see the summary of what a session did (tap again to close) |
| `d` | delete a session |
| `x` | stop a session's tmux process |
| `r` | refresh |
| `?` | help · `q` quit |

You don't need to remember these — press `?` inside the app anytime.

## Command-line

```bash
codex-sm            # interactive UI (default)
codex-sm list       # list sessions
codex-sm status <id>
codex-sm attach <id>
codex-sm new [prompt]
codex-sm delete <id>
```

## Requirements

- Python 3.10+ (no pip dependencies)
- `tmux` (for detachable sessions)
- `codex` CLI
