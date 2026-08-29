"""Auto-summarizer for finished Codex sessions.

When a session transitions running -> ready, a temporary summarizer Codex
session is created (`codex exec --json`), fed the session's final response with
a fixed prompt, and its output is stored as a sidecar summary "attached" to
that session. The summarizer session is then deleted, leaving no trace except
the summary file.

Storage (all under $CODEX_HOME):
  sm_summaries/<id>.txt      — the finished summary
  sm_summaries/<id>.pending  — marker while summarizing
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


def _home(home: str | None) -> str:
    return home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def summaries_dir(home: str | None = None) -> str:
    return os.path.join(_home(home), "sm_summaries")


def summary_path(sess_id: str, home: str | None = None) -> str:
    return os.path.join(summaries_dir(home), sess_id + ".txt")


def pending_path(sess_id: str, home: str | None = None) -> str:
    return os.path.join(summaries_dir(home), sess_id + ".pending")


# Summaries are always on for every session; there is no per-session toggle.


# ---------------------------------------------------------------- final response

def get_final_response(rollout_path: str | None) -> str:
    """Extract the session's final response from its rollout JSONL.

    Prefers the last `task_complete` event's `last_agent_message`; falls back to
    the last few assistant messages.
    """
    if not rollout_path or not os.path.exists(rollout_path):
        return ""
    last_msg: str | None = None
    assistant: list[str] = []
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
                t = obj.get("type")
                if t == "event_msg":
                    p = obj.get("payload") or {}
                    if p.get("type") == "task_complete":
                        m = p.get("last_agent_message")
                        if m:
                            last_msg = m
                elif t == "response_item":
                    p = obj.get("payload") or {}
                    if p.get("type") == "message" and p.get("role") == "assistant":
                        for c in p.get("content") or []:
                            tx = c.get("text") if isinstance(c, dict) else None
                            if tx:
                                assistant.append(tx)
    except OSError:
        return ""
    if last_msg:
        return last_msg
    if assistant:
        return "\n\n".join(assistant[-3:])
    return ""


# ---------------------------------------------------------------- state

def summary_state(sess_id: str, home: str | None = None) -> str:
    """One of: 'done', 'in_progress', 'none'.  Summaries are always on."""
    if os.path.exists(summary_path(sess_id, home)):
        return "done"
    if os.path.exists(pending_path(sess_id, home)):
        return "in_progress"
    return "none"


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
    """Remove the stored summary (and pending) for a session."""
    for p in (summary_path(sess_id, home), pending_path(sess_id, home)):
        try:
            os.remove(p)
        except OSError:
            pass


def trigger(sess_id: str, rollout_path: str | None, home: str | None = None) -> None:
    """Start a background summarizer for the given session (non-blocking).

    Idempotent: does nothing if already done or in progress.
    """
    if summary_state(sess_id, home) in ("done", "in_progress"):
        return
    content = get_final_response(rollout_path)
    if not content:
        return
    os.makedirs(summaries_dir(home), exist_ok=True)
    # mark in-progress
    try:
        open(pending_path(sess_id, home), "w").close()
    except OSError:
        return
    threading.Thread(
        target=_run_summarizer, args=(sess_id, content, home), daemon=True
    ).start()


def _run_summarizer(sess_id: str, content: str, home: str | None) -> None:
    prompt = (
        SUMMARY_INSTRUCTION
        + "\n\n<result>\n" + content + "\n</result>\n\n"
        + "Reply with only the summary, no preamble or extra commentary."
    )
    thread_id: str | None = None
    summary_parts: list[str] = []
    try:
        proc = subprocess.run(
            ["codex", "exec", "--json"],
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
            except OSError:
                pass
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        # Delete the temporary summarizer Codex session.
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
        # Clear the in-progress marker whether or not a summary was produced.
        try:
            os.remove(pending_path(sess_id, home))
        except OSError:
            pass
