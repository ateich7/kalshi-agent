#!/usr/bin/env python
"""
Aggregate data/paper_trades.csv into the numbers kalshi_agent_dashboard.html
embeds. Run this, then paste the printed JSON into the <script id="data">
block in that file, then republish via the Artifact tool (on-request, not
auto-refreshed — see project CLAUDE.md).
"""

import csv
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from record_trade import LEDGER_PATH  # noqa: E402


def build():
    if not LEDGER_PATH.exists():
        rows = []
    else:
        with open(LEDGER_PATH, newline='') as f:
            rows = list(csv.DictReader(f))

    closed = [r for r in rows if r['status'] == 'closed']
    open_rows = [r for r in rows if r['status'] == 'open']

    closed_sorted = sorted(closed, key=lambda r: r['closed_at'])
    cumulative = []
    running = 0.0
    for r in closed_sorted:
        running += float(r['pnl'])
        cumulative.append({'closed_at': r['closed_at'], 'cumulative_pnl': round(running, 2)})

    wins = sum(1 for r in closed if float(r['pnl']) > 0)
    win_rate = (wins / len(closed)) if closed else None

    by_series = {}
    for r in closed:
        s = r['series_ticker'] or 'unknown'
        by_series.setdefault(s, {'n': 0, 'pnl': 0.0})
        by_series[s]['n'] += 1
        by_series[s]['pnl'] += float(r['pnl'])
    by_series = sorted(
        ({'series': k, **v} for k, v in by_series.items()),
        key=lambda x: -x['pnl'],
    )

    data = {
        'total_trades': len(rows),
        'open_count': len(open_rows),
        'closed_count': len(closed),
        'win_rate': win_rate,
        'cumulative_pnl': round(running, 2),
        'cumulative_series': cumulative,
        'by_series': by_series,
        'recent': sorted(rows, key=lambda r: r['opened_at'], reverse=True)[:20],
    }
    print(json.dumps(data, indent=2))


if __name__ == '__main__':
    build()
