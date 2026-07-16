#!/usr/bin/env python
"""
Deterministic settlement + P&L pass over data/paper_trades.csv.

Deliberately plain Python, no LLM involved — financial arithmetic on the
ledger should be auditable and exactly reproducible, not something an LLM
recomputes each cycle. Run this before invoking the agent each cycle (see
agent_loop.sh) so it always reasons from an up-to-date ledger.

For each `status=open` row: looks up the market via the public (unauthenticated,
no-key) API. If it has settled — `status == 'finalized'` with a non-empty
`result` — computes hypothetical P&L using contracts-based math:
    contracts = hypothetical_size / entry_price
    won       = (side == result)
    pnl       = contracts - hypothetical_size   if won   (payout $1/contract)
              = -hypothetical_size              if lost
and marks the row closed. Still-open markets are left untouched.
"""

import csv
from pathlib import Path

from kalshi_public import KalshiPublicClient
from record_trade import FIELDS, LEDGER_PATH


def resolve():
    if not LEDGER_PATH.exists():
        print('no ledger yet, nothing to resolve')
        return

    with open(LEDGER_PATH, newline='') as f:
        rows = list(csv.DictReader(f))

    client = KalshiPublicClient()
    n_checked = n_closed = 0

    for row in rows:
        if row['status'] != 'open':
            continue
        n_checked += 1
        market = client.get_market(row['market_ticker'])
        if market.get('status') != 'finalized' or not market.get('result'):
            continue

        result = market['result']
        entry_price = float(row['entry_price'])
        size = float(row['hypothetical_size'])
        contracts = size / entry_price
        won = row['side'] == result
        pnl = (contracts - size) if won else -size

        row['status'] = 'closed'
        row['closed_at'] = market.get('updated_time', '')
        row['settlement_result'] = result
        row['pnl'] = f'{pnl:.4f}'
        n_closed += 1

    if n_closed:
        with open(LEDGER_PATH, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)

    print(f'resolve_trades: checked {n_checked} open position(s), '
          f'closed {n_closed}')


if __name__ == '__main__':
    resolve()
