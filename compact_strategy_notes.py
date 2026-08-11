#!/usr/bin/env python3
"""Deterministic, non-LLM rotation for strategy_notes.md.

strategy_notes.md is the only file the agent's `claude -p` prompt is
instructed to "read in full" every cycle (see agent_loop.sh's PROMPT step
1). Left unbounded it grows forever -- it reached 1.97MB / 9,432 lines by
2026-08-11, and re-reading that in full every cycle (via multiple Read
calls, since the Read tool pages at 2000 lines) is the dominant driver
behind repeated Claude usage-limit exhaustion (see CLAUDE.md's "Token/
usage-limit protection"). The agent condensed its own backlog once, early
on (cycles 1-34), then never did it again -- flagged as "overdue" for
hundreds of cycles. Relying on the agent to self-police this isn't
reliable, so this script makes the bound mechanical instead.

Runs at the top of every agent_loop.sh cycle, before claude -p. Keeps the
live file bounded to the most recent KEEP_ENTRIES dated log entries --
nothing is lost, older entries are appended verbatim to
data/strategy_notes_archive.md (gitignored, server-local, same treatment as
the rest of data/), which the agent can grep/read on demand via
Bash(python3:*) if it ever needs older precedent, but no longer pays to
re-read automatically every cycle.
"""
import datetime
import os
import re

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
NOTES_PATH = os.path.join(REPO_ROOT, "strategy_notes.md")
ARCHIVE_PATH = os.path.join(REPO_ROOT, "data", "strategy_notes_archive.md")

ENTRY_HEADER_RE = re.compile(r"^### .*$", re.MULTILINE)
COMPACTION_NOTE_RE = re.compile(
    r"\n_\d+ older entries archived.*?compaction\._\n", re.DOTALL
)

# Trigger compaction once there are more than this many dated log entries;
# trim back down to KEEP_ENTRIES when it fires. The gap between the two
# avoids rewriting the file every single cycle right at the boundary.
TRIGGER_ENTRIES = 20
KEEP_ENTRIES = 12
MIN_KEEP_ENTRIES = 5
MAX_KEEP_BYTES = 150_000  # safety net in case entries get much more verbose


def main():
    if not os.path.exists(NOTES_PATH):
        return
    with open(NOTES_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    matches = list(ENTRY_HEADER_RE.finditer(text))
    if len(matches) <= TRIGGER_ENTRIES:
        return

    preamble = text[: matches[0].start()]
    entries = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append(text[start:end])

    keep_count = KEEP_ENTRIES
    while (
        keep_count > MIN_KEEP_ENTRIES
        and sum(len(e) for e in entries[-keep_count:]) > MAX_KEEP_BYTES
    ):
        keep_count -= 1

    to_keep = entries[-keep_count:]
    to_archive = entries[:-keep_count]

    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write("".join(to_archive))

    preamble = COMPACTION_NOTE_RE.sub("", preamble)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    note = (
        f"\n_{len(to_archive)} older entries archived to "
        f"data/strategy_notes_archive.md by compact_strategy_notes.py, "
        f"most recently on {today} -- grep/read that file via "
        f"Bash(python3:*) if you need older precedent; don't re-read it by "
        f"default, that's the whole point of this compaction._\n"
    )

    with open(NOTES_PATH, "w", encoding="utf-8") as f:
        f.write(preamble.rstrip("\n") + "\n" + note + "".join(to_keep))

    print(
        f"compact_strategy_notes: archived {len(to_archive)} entries "
        f"({len(text)} bytes -> live file now {os.path.getsize(NOTES_PATH)} bytes)"
    )


if __name__ == "__main__":
    main()
