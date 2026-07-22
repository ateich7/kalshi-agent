"""
Kalshi PUBLIC market-data client — unauthenticated, read-only, by construction.

This module intentionally contains NO key loading, NO request signing, and NO
write endpoints (no place_order, no cancel_order, no _post). It only ever
issues GET requests to Kalshi's unauthenticated market-data host. There is no
credential anywhere in this file capable of authenticating a trade — that is
the whole point. See kalshi-agent/CLAUDE.md for why this matters.

Confirmed against Kalshi's own docs (docs.kalshi.com/getting_started/
quick_start_market_data, fetched 2026-07-15): "No authentication headers are
required for the endpoints in this guide," and this unauthenticated host
serves ALL Kalshi markets (politics, sports, economics, climate, entertainment,
etc.), not just elections despite the "elections" naming on the signed prod
host used by other sibling projects.

Usage:
    from kalshi_public import KalshiPublicClient
    k = KalshiPublicClient()
    markets = k.get_markets(series_ticker='KXHIGHNY', status='open')
"""

import json
import time
import urllib.request
import urllib.parse
import urllib.error


BASE_URL = 'https://external-api.kalshi.com/trade-api/v2'

# Empirically observed 2026-07-15: unauthenticated calls get HTTP 429 after
# roughly a couple dozen rapid-fire requests. Retry with backoff, and pace
# paginated loops so a full scan doesn't trip the limit in the first place.
MAX_RETRIES = 5
PAGE_DELAY_SECONDS = 0.3

# Fallback list used only if a categoryless /series call doesn't return
# everything (verify empirically — see kalshi-agent/CLAUDE.md verification
# notes). Kalshi's category taxonomy as observed on kalshi.com.
KNOWN_CATEGORIES = [
    'Politics', 'Sports', 'Economics', 'Financials', 'Climate and Weather',
    'Science and Technology', 'Health', 'Entertainment', 'Companies',
    'Crypto', 'World', 'Culture',
]


class KalshiPublicClient:
    """Unauthenticated, GET-only Kalshi market data client. No key, no writes."""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url

    # -- HTTP -----------------------------------------------------------------

    def _get(self, path: str, params=None) -> dict:
        url = self.base_url + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors='ignore')
                if e.code == 429 and attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f'GET {path} -> {e.code}: {body}') from e

    def _iter_pages(self, path: str, result_key: str, params=None):
        """Generator variant of _get_all_pages — yields items one at a time
        instead of accumulating the full result list in memory. Use this for
        result sets that can be large (e.g. all open markets platform-wide,
        tens of thousands at once) — see iter_markets/iter_all_open_markets."""
        cursor = None
        while True:
            p = dict(params or {})
            p.setdefault('limit', 200)
            if cursor:
                p['cursor'] = cursor
            data = self._get(path, p)
            items = data.get(result_key, []) or []
            for item in items:
                yield item
            cursor = data.get('cursor')
            if not cursor or not items:
                break
            time.sleep(PAGE_DELAY_SECONDS)

    def _get_all_pages(self, path: str, result_key: str, params=None) -> list:
        return list(self._iter_pages(path, result_key, params))

    # -- Market discovery -------------------------------------------------------

    def get_series(self, category: str = None) -> list:
        """List series, optionally filtered to one category. No category ->
        attempts a single categoryless call first; if that comes back empty,
        falls back to unioning every category in KNOWN_CATEGORIES."""
        if category:
            return self._get_all_pages('/series', 'series', {'category': category})

        data = self._get_all_pages('/series', 'series')
        if data:
            return data

        seen, out = set(), []
        for cat in KNOWN_CATEGORIES:
            for s in self._get_all_pages('/series', 'series', {'category': cat}):
                if s.get('ticker') not in seen:
                    seen.add(s.get('ticker'))
                    out.append(s)
        return out

    def get_events(self, series_ticker: str = None, status: str = 'open') -> list:
        """List events, optionally filtered to a series (default: open only)."""
        params = {'status': status}
        if series_ticker:
            params['series_ticker'] = series_ticker
        return self._get_all_pages('/events', 'events', params)

    def get_markets(self, series_ticker: str = None, event_ticker: str = None,
                     status: str = 'open', max_close_ts: int = None,
                     min_close_ts: int = None) -> list:
        """List markets, optionally filtered by series/event/close-time window.

        `max_close_ts`/`min_close_ts` (Unix seconds) bound results by close
        time — the main lever for keeping a platform-wide scan tractable,
        since there are tens of thousands of open markets at any moment."""
        params = {'status': status, 'limit': 1000}
        if series_ticker:
            params['series_ticker'] = series_ticker
        if event_ticker:
            params['event_ticker'] = event_ticker
        if max_close_ts:
            params['max_close_ts'] = max_close_ts
        if min_close_ts:
            params['min_close_ts'] = min_close_ts
        return self._get_all_pages('/markets', 'markets', params)

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker (works for settled markets too —
        includes `status` and `result` fields once resolved). Note: a
        resolved market's `status` is `'finalized'`, not `'settled'`
        (verified empirically 2026-07-15) — `result` is `'yes'`/`'no'`."""
        return self._get(f'/markets/{ticker}').get('market', {})

    def get_orderbook(self, ticker: str) -> dict:
        """Fetch the current orderbook for a single market.

        Returns {'yes_dollars': [[price_str, size_str], ...], 'no_dollars': [...]}
        — response key is `orderbook_fp` on the wire (fixed-point dollars),
        not `orderbook`; verified empirically 2026-07-15."""
        return self._get(f'/markets/{ticker}/orderbook').get('orderbook_fp', {})

    def get_all_open_markets(self, close_within_seconds: int = None) -> list:
        """Every open market platform-wide (all series/categories), via a
        single paginated /markets?status=open call chain (NOT a per-series
        loop — empirically ~tens of thousands of open markets exist at any
        moment across ~12k series, so looping per-series is far too slow and
        trips the unauthenticated rate limit).

        `close_within_seconds`, if given, bounds results to markets closing
        soon via `max_close_ts` — the practical way to keep a scan tractable
        for both the rate limit and an LLM's context window. Omit for an
        unbounded (slow, large) full-platform scan.

        This materializes the full result list — as of 2026-07-22 that's
        ~60,000+ raw market dicts (up from ~9,000 at 2026-07-16 launch),
        enough to OOM-kill a caller on a small box. Prefer
        iter_all_open_markets for anything processing the platform-wide set."""
        max_close_ts = None
        if close_within_seconds is not None:
            max_close_ts = int(time.time()) + close_within_seconds
        return self.get_markets(status='open', max_close_ts=max_close_ts)

    def iter_markets(self, series_ticker: str = None, event_ticker: str = None,
                      status: str = 'open', max_close_ts: int = None,
                      min_close_ts: int = None):
        """Generator variant of get_markets — yields one market dict at a
        time instead of holding the whole result list in memory. Prefer this
        over get_markets/get_all_open_markets for platform-wide (no
        series_ticker) queries, where the result set can be tens of
        thousands of markets."""
        params = {'status': status, 'limit': 1000}
        if series_ticker:
            params['series_ticker'] = series_ticker
        if event_ticker:
            params['event_ticker'] = event_ticker
        if max_close_ts:
            params['max_close_ts'] = max_close_ts
        if min_close_ts:
            params['min_close_ts'] = min_close_ts
        return self._iter_pages('/markets', 'markets', params)

    def iter_all_open_markets(self, close_within_seconds: int = None):
        """Generator variant of get_all_open_markets — see iter_markets.
        This is the one fetch_snapshot.py uses: it lets the caller stream
        each market to disk and keep only a bounded top-N in memory, instead
        of holding the full ~60k-market platform-wide list at once (the
        cause of the 2026-07-21/22 OOM-kill outage on this box)."""
        max_close_ts = None
        if close_within_seconds is not None:
            max_close_ts = int(time.time()) + close_within_seconds
        return self.iter_markets(status='open', max_close_ts=max_close_ts)

    # -- Pricing ----------------------------------------------------------------

    @staticmethod
    def implied_prob(market: dict):
        """
        Best-estimate implied YES probability from market bid/ask.

        Priority: bid/ask midpoint -> last traded price -> integer cent
        fallbacks. Returns None if no price data at all.
        """
        def to_f(v):
            try:
                return float(v) if v not in (None, '', '0.0000') else None
            except (TypeError, ValueError):
                return None

        bid = to_f(market.get('yes_bid_dollars'))
        ask = to_f(market.get('yes_ask_dollars'))
        if bid is not None and ask is not None and ask > 0:
            return (bid + ask) / 2
        lp = to_f(market.get('last_price_dollars'))
        if lp is not None:
            return lp
        bid_c = market.get('yes_bid')
        ask_c = market.get('yes_ask')
        if bid_c is not None and ask_c is not None and ask_c > 0:
            return (bid_c + ask_c) / 2 / 100
        lp_c = market.get('last_price')
        if lp_c is not None:
            return lp_c / 100
        return None
