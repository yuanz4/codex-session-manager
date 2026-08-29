"""Auto-summarizer for Codex sessions.

Summaries are always on. For every session:

  - At manager startup, any session missing a summary is summarized (in
    parallel background threads).
  - During an ongoing conversation, the summary is updated each time a turn
    completes *normally* — so it reflects the latest state. A turn that ends
    normally is one whose rollout contains a `task_complete` event that is not
    immediately preceded by an `error`/interrupt. Interrupted or errored turns
    are NOT summarized (nor do they trigger an update).

A summary is produced by a throwaway `codex exec --json` session fed the
session's most recent normal-completion response under a fixed prompt; that
summarizer session is then deleted, leaving only a sidecar summary.

Storage (all under $CODEX_HOME):
  sm_summaries/<id>.txt      — the finished summary
  sm_summaries/<id>.pending  — marker while summarizing
  sm_summaries/<id>.turn     — id of the last turn summarized (for updates)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading

SUMMARIZER_MARKER = "You should summarize the codex result."

SUMMARY_INSTRUCTION = (
    "You should summarize the codex result. You should summarize it to be brief "
    "so users can understand the most important ideas, and later they can choose "
    "to view the details. The summary can't be too long, and should be easy to "
    "view in a limited size."
)

EXEC_TIMEOUT = 240  # seconds
MAX_PARALLEL = 4  # concurrent summarizers at startup

# event types that mark a turn as NOT a normal completion
ABNORMAL_EVENTS = {"error", "task_interrupted", "turn_interrupted"}


def _home(home: str | None) -> str:
    return home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def summaries_dir(home: str | None = None) -> str:
    return os.path.join(_home(home), "sm_summaries")


def summary_path(sess_id: str, home: str | None = None) -> str:
    return os.path.join(summaries_dir(home), sess_id + ".txt")


def pending_path(sess_id: str, home: str | None = None) -> str:
    return os.path.join(summaries_dir(home), sess_id + ".pending")


def turn_path(sess_id: str, home: str | None = None) -> str:
    return os.path.join(summaries_dir(home), sess_id + ".turn")


# Summaries are always on for every session; there is no per-session toggle.


# ---------------------------------------------------------------- rollout analysis

def analyze_rollout(rollout_path: str | None) -> dict:
    """Scan a rollout JSONL and return the latest normally-completed turn.

    Returns a dict:
      { "status": running|ready|error,
        "last_complete_turn": <turn_id or None>,
        "last_message": <str or None>,
        "abnormal": True if the last turn ended via error/interrupt }
    A turn completes normally when its `task_complete` event is present and not
    immediately preceded by an abnormal event (error/interrupt) for that turn.
    """
    out = {"status": "ready", "last_complete_turn": None, "last_message": None, "abnormal": False}
    if not rollout_path or not os.path.exists(rollout_path):
        return out
    started_turns: set[str] = set()
    completed_turns: dict[str, str | None] = {}  # turn_id -> last_agent_message
    abnormal_turns: set[str] = set()
    last_event: str | None = None
    last_turn_seen: str | None = None
    try:
        with open(rollout_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "event_msg":
                    continue
                p = obj.get("payload") or {}
                et = p.get("type")
                if et is None:
                    continue
                last_event = et
                tid = p.get("turn_id")
                if tid:
                    last_turn_seen = tid
                if et == "task_started":
                    if tid:
                        started_turns.add(tid)
                elif et == "task_complete":
                    if tid:
                        # A normal completion only if this turn was not flagged abnormal.
                        if tid not in abnormal_turns:
                            completed_turns[tid] = p.get("last_agent_message")
                elif et in ABNORMAL_EVENTS:
                    # attribute the abnormal end to the most recent started turn
                    if tid and tid in started_turns:
                        abnormal_turns.add(tid)
                    elif last_turn_seen:
                        abnormal_turns.add(last_turn_seen)
    except OSError:
        return out

    # status: error if the terminal event is an error; running if an open turn
    # has no completion; else ready.
    if last_event in ABNORMAL_EVENTS:
        out["status"] = "error"
        out["abnormal"] = True
    else:
        # last started turn without a completion -> running
        open_turns = [t for t in started_turns if t not in completed_turns and t not in abnormal_turns]
        if open_turns:
            out["status"] = "running"
    # latest normally-completed turn (by occurrence order; dict preserves insertion order)
    last_t = None
    last_msg = None
    for t, msg in completed_turns.items():
        last_t = t
        last_msg = msg
    out["last_complete_turn"] = last_t
    out["last_message"] = last_msg
    return out


# ---------------------------------------------------------------- state

def summary_state(sess_id: str, home: str | None = None) -> str:
    """One of: 'done', 'in_progress', 'none'.  Summaries are always on."""
    if os.path.exists(summary_path(sess_id, home)):
        return "done"
    if os.path.exists(pending_path(sess_id, home)):
        return "in_progress"
    return "none"


def last_summarized_turn(sess_id: str, home: str | None = None) -> str | None:
    try:
        with open(turn_path(sess_id, home), encoding="utf-8", errors="replace") as f:
            return f.read().strip() or None
    except OSError:
        return None


def read_summary(sess_id: str, home: str | None = None) -> str | None:
    try:
        with open(summary_path(sess_id, home), encoding="utf-8", errors="replace") as f:
            return f.read().strip() or None
    except OSError:
        return None


def cleanup_stale_pending(home: str | None = None) -> None:
    """Remove .pending markers left by a previous (now-dead) process."""
    d = summaries_dir(home)
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        if name.endswith(".pending"):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


def reset_summary(sess_id: str, home: str | None = None) -> None:
    """Remove the stored summary (and markers) for a session."""
    for p in (summary_path(sess_id, home), pending_path(sess_id, home), turn_path(sess_id, home)):
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------- triggering

def trigger(sess_id: str, rollout_path: str | None, home: str | None = None, force: bool = False) -> bool:
    """Start a background summarizer for the latest normally-completed turn.

    Skips (returns False) if:
      - already done/in_progress and not forced
      - no normally-completed turn exists (still running, or ended abnormally)
      - the latest completed turn was already summarized (unless force)

    Returns True if a summarizer was started.
    """
    info = analyze_rollout(rollout_path)
    turn = info["last_complete_turn"]
    msg = info["last_message"]
    if not turn or not msg:
        return False
    if not force:
        if summary_state(sess_id, home) == "in_progress":
            return False
        if last_summarized_turn(sess_id, home) == turn and os.path.exists(summary_path(sess_id, home)):
            return False  # already summarized this turn
    os.makedirs(summaries_dir(home), exist_ok=True)
    try:
        open(pending_path(sess_id, home), "w").close()
    except OSError:
        return False
    threading.Thread(
        target=_run_summarizer, args=(sess_id, turn, msg, home), daemon=True
    ).start()
    return True


def summarize_all_missing(sessions, home: str | None = None) -> None:
    """Launch parallel summarizers for every session lacking a summary.

    `sessions` is an iterable of objects with `.id`, `.rollout_path`,
    `.status` attributes (e.g. codex_sm.sessions.Session). Sessions that are
    currently running are skipped (no completed turn yet). Throttled to
    MAX_PARALLEL concurrent summarizers.
    """
    sem = threading.Semaphore(MAX_PARALLEL)

    def run(sess):
        sem.acquire()
        try:
            trigger(sess.id, sess.rollout_path, home)
        finally:
            sem.release()

    threads = []
    for sess in sessions:
        if getattr(sess, "status", None) == "running":
            continue
        if summary_state(sess.id, home) == "none":
            t = threading.Thread(target=run, args=(sess,), daemon=True)
            t.start()
            threads.append(t)


def maybe_update(state_sessions, home: str | None = None) -> None:
    """Re-summarize sessions whose latest completed turn changed since last summary.

    Called on each refresh. For each session, if it's ready (or error-but-had-a-
    prior-normal-turn) and has a new normally-completed turn vs. what we last
    summarized, trigger an update.
    """
    for sess in state_sessions:
        info = analyze_rollout(sess.rollout_path)
        turn = info["last_complete_turn"]
        if not turn:
            continue
        if last_summarized_turn(sess.id, home) == turn and os.path.exists(summary_path(sess.id, home)):
            continue
        if summary_state(sess.id, home) == "in_progress":
            continue
        trigger(sess.id, sess.rollout_path, home, force=True)


def _run_summarizer(sess_id: str, turn: str, content: str, home: str | None) -> None:
    prompt = (
        SUMMARY_INSTRUCTION
        + "\n\n<result>\n" + content + "\n</result>\n\n"
        + "Reply with only the summary, no preamble or extra commentary."
    )
    thread_id: str | None = None
    summary_parts: list[str] = []
    try:
        proc = subprocess.run(
            ["codex", "exec", "--json", "--skip-git-repo-check"],
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=EXEC_TIMEOUT,
        )
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "thread.started":
                thread_id = obj.get("thread_id")
            elif obj.get("type") == "item.completed":
                item = obj.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    summary_parts.append(item["text"])
        if summary_parts:
            summary = "\n".join(summary_parts).strip()
            try:
                with open(summary_path(sess_id, home), "w", encoding="utf-8") as f:
                    f.write(summary)
                with open(turn_path(sess_id, home), "w", encoding="utf-8") as f:
                    f.write(turn)
            except OSError:
                pass
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        if thread_id:
            try:
                subprocess.run(
                    ["codex", "delete", "--force", thread_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            os.remove(pending_path(sess_id, home))
        except OSError:
            pass
