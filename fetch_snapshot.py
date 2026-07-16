#!/usr/bin/env python
"""
Pull a fresh cross-market snapshot and write it to data/snapshots/.

There are ~9,000+ open markets closing within just the next 3 days platform-
wide (measured empirically 2026-07-15), across every category Kalshi offers —
far more than fits an LLM's context window or is worth reasoning about in one
cycle. So this script:

  1. Fetches everything closing within --close-within-hours (default 72h) —
     bounds the platform-wide scan to a tractable, timely slice via the
     public API's `max_close_ts` filter (kalshi_public.get_all_open_markets).
  2. Archives the FULL raw result to data/snapshots/<timestamp>_full.jsonl
     (so nothing is thrown away — useful for later backtesting/analysis).
  3. Writes a CURATED top --top-n (default 150) subset, ranked by 24h volume,
     trimmed to only the fields worth reasoning over, to
     data/snapshots/latest.json — this is the file the agent actually reads
     each cycle via the Read tool (it has no Bash access to process JSON, by
     design, so this file needs to already be small and plain enough to just
     read). Sorting by volume favors markets with real two-sided trading
     activity (where mispricing is more likely to mean something) over the
     long tail of near-dead, zero-volume markets.

No Kalshi credential of any kind is used or required — see kalshi_public.py.
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_public import KalshiPublicClient

SNAPSHOT_DIR = Path(__file__).parent / 'data' / 'snapshots'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--close-within-hours', type=float, default=72)
    ap.add_argument('--top-n', type=int, default=150)
    args = ap.parse_args()

    client = KalshiPublicClient()
    t0 = time.time()
    markets = client.get_all_open_markets(
        close_within_seconds=int(args.close_within_hours * 3600))
    elapsed = time.time() - t0

    for m in markets:
        m['implied_prob'] = client.implied_prob(m)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    full_path = SNAPSHOT_DIR / f'{ts}_full.jsonl'
    with open(full_path, 'w') as f:
        for m in markets:
            f.write(json.dumps(m) + '\n')

    def vol(m):
        try:
            return float(m.get('volume_24h_fp', 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    KEEP_FIELDS = (
        'ticker', 'event_ticker', 'title', 'yes_sub_title', 'no_sub_title',
        'status', 'close_time', 'implied_prob', 'yes_bid_dollars',
        'yes_ask_dollars', 'volume_24h_fp', 'liquidity_dollars',
    )
    curated_full = sorted(markets, key=vol, reverse=True)[:args.top_n]
    curated = [{k: m.get(k) for k in KEEP_FIELDS} for m in curated_full]
    latest_path = SNAPSHOT_DIR / 'latest.json'
    with open(latest_path, 'w') as f:
        json.dump({
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'close_within_hours': args.close_within_hours,
            'total_markets_seen': len(markets),
            'curated_count': len(curated),
            'markets': curated,
        }, f, separators=(',', ':'))

    print(f'fetch_snapshot: {len(markets)} markets closing within '
          f'{args.close_within_hours}h ({elapsed:.1f}s) -> {full_path.name}; '
          f'curated top {len(curated)} by volume -> latest.json')


if __name__ == '__main__':
    main()
