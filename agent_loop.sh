#!/bin/bash
# One cycle of the paper-trading research agent. Invoked periodically by
# systemd (kalshi-agent-cycle.timer -> kalshi-agent-cycle.service on the
# server). Safe to run manually too.
#
# Order matters: resolve settlements and fetch a fresh snapshot with plain
# Python BEFORE invoking Claude, so the LLM never needs Bash access to the
# network or to these scripts — only to record_trade.py (see
# --allowedTools below and .claude/settings.json).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "=== $(date -u +%FT%TZ) agent_loop starting ==="

python resolve_trades.py
python fetch_snapshot.py --close-within-hours 72 --top-n 300

PROMPT=$(cat <<'EOF'
You are running one cycle of an ongoing Kalshi paper-trading research loop.
You are paper-trading ONLY. There is no Kalshi API key anywhere in this
environment — you have no ability to place a real order and must never try
to construct one (e.g. by writing your own signed HTTP request). Your only
write actions this cycle are: (1) calling record_trade.py to log a
hypothetical trade, and (2) editing strategy_notes.md.

Do this, in order:

1. Read strategy_notes.md in full — it's your memory from every prior cycle.
2. Read data/paper_trades.csv — your ledger. Rows already settled (status=
   closed) have real Kalshi settlement outcomes and a computed pnl; treat
   that as ground truth for how good your past calls actually were.
3. Read data/snapshots/latest.json — today's curated snapshot: the highest-
   24h-volume markets closing in the next 72 hours, across every Kalshi
   category (not just sports), each with an `implied_prob` computed from the
   current bid/ask midpoint.
4. Decide whether any markets look mispriced relative to your own estimate
   of the true probability, given whatever domain reasoning applies (this
   could be sports, weather, economic releases, entertainment, anything).
   For each one you're confident in, run:
     python record_trade.py --ticker <TICKER> --side yes|no --price <0-1> \
       --size <hypothetical dollars, keep it in a consistent $5-$50 range
       so P&L stays comparable across trades> --rationale "<why>"
   Quality over quantity — recording a trade you don't actually believe in
   just to have activity this cycle makes your own track record less useful
   to you. Zero new trades this cycle is a fine outcome.
5. Update strategy_notes.md's Log section: append a dated entry (a) noting
   what you looked at and decided this cycle, and (b) reflecting honestly on
   whatever previously-closed trades resolved since last cycle — were you
   right, and why or why not? The goal is that your read on any given
   category gets better over time, not just that you accumulate trades.
EOF
)

claude -p "$PROMPT" \
  --allowedTools "Bash(python record_trade.py:*),Read,Edit(strategy_notes.md)" \
  --append-system-prompt "Paper-trading only. No Kalshi credential exists in this environment; never attempt to place a real order."

echo "=== $(date -u +%FT%TZ) agent_loop done ==="
