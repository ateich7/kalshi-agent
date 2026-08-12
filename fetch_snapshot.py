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
  2. Archives the FULL raw result to data/snapshots/<timestamp>_full.jsonl.gz
     (so nothing is thrown away — useful for later backtesting/analysis, but
     note nothing in this codebase actually reads these back today; verified
     2026-08-12 that no script — agent-authored or otherwise — references
     `_full.jsonl` anywhere except this file writing it. It's a pure audit
     trail, kept for optionality, not a live dependency).
     Gzipped directly (not gzipped after the fact) because platform-wide
     open-market count has been growing fast (61k -> 880k+ observed by
     2026-08-12) and raw JSONL compresses ~20x — writing uncompressed first
     briefly drove data/snapshots/ to 11GB against a 24GB disk (96% full,
     1.2GB free) before the gzip fix. Also prunes archives older than
     --archive-retention-days each run so this stays bounded even if market
     count keeps climbing. Default lowered 14 -> 2 on 2026-08-12: even
     gzipped, individual archives now regularly hit 100-200MB+ at current
     market-count levels, and 14 days of that reflooded the disk to 94% full
     (10GB across 347 archives) despite the gzip fix already landing. Since
     nothing reads these back (see above), there's no real cost to keeping
     the window short — 2 days is plenty of runway to grab a recent archive
     by hand if a future backtest ever actually needs one.
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
import gzip
import heapq
import itertools
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_public import KalshiPublicClient

SNAPSHOT_DIR = Path(__file__).parent / 'data' / 'snapshots'

KEEP_FIELDS = (
    'ticker', 'event_ticker', 'title', 'yes_sub_title', 'no_sub_title',
    'status', 'close_time', 'implied_prob', 'yes_bid_dollars',
    'yes_ask_dollars', 'volume_24h_fp', 'liquidity_dollars',
)


def _vol(m):
    try:
        return float(m.get('volume_24h_fp', 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--close-within-hours', type=float, default=72)
    ap.add_argument('--top-n', type=int, default=150)
    ap.add_argument('--archive-retention-days', type=float, default=2)
    args = ap.parse_args()

    client = KalshiPublicClient()
    t0 = time.time()

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    full_path = SNAPSHOT_DIR / f'{ts}_full.jsonl.gz'

    # Stream markets one at a time rather than materializing the full
    # platform-wide result (~60k+ markets as of 2026-07-22, up from ~9k at
    # 2026-07-16 launch) in memory. Holding all of them at once is what
    # OOM-killed this script six times in a row on 2026-07-21/22 (961MB box,
    # anon-rss ~700-723MB observed at each kill). Peak memory here is now one
    # API page (<=1000 raw dicts) plus a --top-n-sized heap, not the total
    # market count.
    counter = itertools.count()
    heap = []  # (volume, tiebreak, market) tuples, kept at size <= top_n
    total_seen = 0

    with gzip.open(full_path, 'wt') as f:
        for m in client.iter_all_open_markets(
                close_within_seconds=int(args.close_within_hours * 3600)):
            m['implied_prob'] = client.implied_prob(m)
            f.write(json.dumps(m) + '\n')
            total_seen += 1

            entry = (_vol(m), next(counter), m)
            if len(heap) < args.top_n:
                heapq.heappush(heap, entry)
            elif entry[0] > heap[0][0]:
                heapq.heapreplace(heap, entry)

    elapsed = time.time() - t0

    curated_full = [m for _, _, m in sorted(heap, reverse=True)]
    curated = [{k: m.get(k) for k in KEEP_FIELDS} for m in curated_full]
    latest_path = SNAPSHOT_DIR / 'latest.json'
    with open(latest_path, 'w') as f:
        json.dump({
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'close_within_hours': args.close_within_hours,
            'total_markets_seen': total_seen,
            'curated_count': len(curated),
            'markets': curated,
        }, f, separators=(',', ':'))

    cutoff = time.time() - args.archive_retention_days * 86400
    pruned = 0
    for old in SNAPSHOT_DIR.glob('*_full.jsonl*'):
        if old != full_path and old.stat().st_mtime < cutoff:
            old.unlink()
            pruned += 1
    if pruned:
        print(f'fetch_snapshot: pruned {pruned} archive(s) older than '
              f'{args.archive_retention_days}d')

    print(f'fetch_snapshot: {total_seen} markets closing within '
          f'{args.close_within_hours}h ({elapsed:.1f}s) -> {full_path.name}; '
          f'curated top {len(curated)} by volume -> latest.json')


if __name__ == '__main__':
    main()
