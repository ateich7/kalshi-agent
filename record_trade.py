#!/usr/bin/env python
"""
Append one hypothetical (paper) trade to data/paper_trades.csv.

This is the ONLY write path into the ledger — the headless agent invokes this
script via Bash rather than editing the CSV directly, so every row is
validated and consistently shaped. This script makes no Kalshi API calls that
require a key (it enriches the row with public market data via
kalshi_public.py) and has no concept of a real order — it only ever writes a
row to a local CSV.

Usage:
    python record_trade.py --ticker KXFAOFPI-26AUG07-T136.0 --side yes \
        --price 0.11 --size 10 --rationale "FAO index momentum suggests..."
"""

import argparse
import csv
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kalshi_public import KalshiPublicClient

LEDGER_PATH = Path(__file__).parent / 'data' / 'paper_trades.csv'
FIELDS = [
    'trade_id', 'opened_at', 'market_ticker', 'event_ticker', 'series_ticker',
    'title', 'side', 'entry_price', 'hypothetical_size', 'rationale',
    'status', 'closed_at', 'settlement_result', 'pnl',
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ticker', required=True, help='Kalshi market ticker')
    ap.add_argument('--side', required=True, choices=['yes', 'no'])
    ap.add_argument('--price', required=True, type=float,
                     help='Hypothetical entry price, dollars (0 < price < 1)')
    ap.add_argument('--size', required=True, type=float,
                     help='Hypothetical position size, in dollars at risk')
    ap.add_argument('--rationale', required=True,
                     help='Why this trade — the strategy signal behind it')
    args = ap.parse_args()

    if not (0 < args.price < 1):
        sys.exit(f'error: --price must be between 0 and 1, got {args.price}')
    if args.size <= 0:
        sys.exit(f'error: --size must be positive, got {args.size}')

    market = KalshiPublicClient().get_market(args.ticker)
    if not market:
        sys.exit(f'error: market {args.ticker!r} not found via public API')
    if market.get('status') not in ('active', 'initialized'):
        sys.exit(f"error: market {args.ticker!r} status is "
                  f"{market.get('status')!r}, not open — refusing to record "
                  f"a paper trade against a non-tradeable market")

    event_ticker = market.get('event_ticker', '')
    row = {
        'trade_id': uuid.uuid4().hex[:12],
        'opened_at': datetime.now(timezone.utc).isoformat(),
        'market_ticker': args.ticker,
        'event_ticker': event_ticker,
        'series_ticker': event_ticker.split('-')[0] if event_ticker else '',
        'title': market.get('title', ''),
        'side': args.side,
        'entry_price': args.price,
        'hypothetical_size': args.size,
        'rationale': args.rationale,
        'status': 'open',
        'closed_at': '',
        'settlement_result': '',
        'pnl': '',
    }

    is_new = not LEDGER_PATH.exists()
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(row)

    print(f"recorded paper trade {row['trade_id']}: {args.side.upper()} "
          f"{args.ticker} @ {args.price} (${args.size} hypothetical)")


if __name__ == '__main__':
    main()
