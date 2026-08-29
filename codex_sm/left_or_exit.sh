#!/usr/bin/env bash
# Called by tmux when Left is pressed inside a Codex session (key-table:
# codex_session). Decides whether to switch back to the codex-sm manager or
# forward the Left keystroke to Codex for normal cursor movement.
#
# We exit to the menu ONLY when the Codex prompt is empty AND the cursor is at
# the prompt's input start. That state is detected as:
#   - cursor_x is at/within the prompt marker (cols 0-2; Codex places cursor at 2)
#   - AND the line under the cursor contains the dim placeholder (ANSI SGR \033[2m)
#     OR, after stripping escapes and whitespace, has no visible text.
# Otherwise we send Left to the pane so editing behaves normally.
set -u

PANE_ID="${1:-}"
MGR="codex-sm"

if [ -z "$PANE_ID" ]; then
    PANE_ID=$(tmux display -p '#{pane_id}' 2>/dev/null)
fi

cur_x="$(tmux display -t "$PANE_ID" -p '#{cursor_x}' 2>/dev/null)"
cur_y="$(tmux display -t "$PANE_ID" -p '#{cursor_y}' 2>/dev/null)"

line="$(tmux capture-pane -t "$PANE_ID" -p -e -S "$cur_y" -E "$cur_y" 2>/dev/null | head -1)"

# Require this to be a Codex prompt line (it carries the '›' marker, in bold).
case "$line" in
    *›*) : ;;  # the prompt marker
    *) tmux send-keys -t "$PANE_ID" Left; exit 0 ;;
esac

# Parse cursor_x defensively.
case "$cur_x" in
    ''|*[!0-9]*) cur_x=999 ;;
esac

# Strip ANSI SGR sequences to recover the visible glyph bytes.
visible="$(printf '%s' "$line" | sed $'s/\x1b\\[[0-9;]*m//g')"

is_empty=0
if [ "$cur_x" -le 2 ]; then
    if printf '%s' "$line" | grep -q $'\x1b\\[2m'; then
        # The text after the prompt marker is dimmed -> it's the placeholder,
        # i.e. the user has typed nothing.
        is_empty=1
    else
        # No placeholder and no visible text beyond the marker -> empty too.
        after_marker="$(printf '%s' "$visible" | sed 's/^[^ ]*[[:space:]]*//')"
        after_marker_trim="$(printf '%s' "$after_marker" | sed 's/[[:space:]]*$//')"
        if [ -z "$after_marker_trim" ]; then
            is_empty=1
        fi
    fi
fi

if [ "$is_empty" -eq 1 ]; then
    tmux switch-client -t "$MGR"
else
    tmux send-keys -t "$PANE_ID" Left
fi
