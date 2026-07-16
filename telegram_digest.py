#!/usr/bin/env python
"""
Daily Telegram digest of the paper-trading ledger. Run on its own daily
cron/timer (separate from the hourly agent_loop.sh cycle).

Reports: trades opened/closed in the last 24h, hypothetical win rate and P&L
over that window, cumulative all-time stats, count of currently-open
positions, and the most recent strategy_notes.md entry (Claude's own summary
of what it's learned lately).
"""

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from record_trade import LEDGER_PATH
from telegram import tg_send

NOTES_PATH = Path(__file__).parent / 'strategy_notes.md'


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def main():
    if not LEDGER_PATH.exists():
        tg_send('[Kalshi Agent] No paper trades recorded yet.')
        return

    with open(LEDGER_PATH, newline='') as f:
        rows = list(csv.DictReader(f))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    opened_24h = [r for r in rows if (parse_ts(r['opened_at']) or now) >= cutoff]
    closed_24h = [r for r in rows if r['status'] == 'closed'
                  and (parse_ts(r['closed_at']) or now) >= cutoff]
    all_closed = [r for r in rows if r['status'] == 'closed']
    open_now = [r for r in rows if r['status'] == 'open']

    def pnl_sum(trades):
        return sum(float(r['pnl']) for r in trades if r['pnl'])

    def win_rate(trades):
        if not trades:
            return None
        wins = sum(1 for r in trades if float(r['pnl'] or 0) > 0)
        return wins / len(trades)

    lines = ['[Kalshi Agent] Daily digest']
    lines.append(f'Last 24h: {len(opened_24h)} opened, {len(closed_24h)} closed')
    if closed_24h:
        wr = win_rate(closed_24h)
        lines.append(f'  24h hypothetical P&L: ${pnl_sum(closed_24h):+.2f} '
                      f'(win rate {wr:.0%})')
    lines.append(f'All-time: {len(all_closed)} closed, {len(open_now)} open')
    if all_closed:
        wr_all = win_rate(all_closed)
        lines.append(f'  Cumulative hypothetical P&L: ${pnl_sum(all_closed):+.2f} '
                      f'(win rate {wr_all:.0%})')

    if NOTES_PATH.exists():
        notes = NOTES_PATH.read_text().strip()
        if notes:
            tail = notes[-400:]
            lines.append('')
            lines.append('Latest strategy notes:')
            lines.append(tail)

    tg_send('\n'.join(lines))
    print('telegram_digest: sent')


if __name__ == '__main__':
    main()
