# Kalshi Paper-Trading Research Agent

## Maintaining this file

If anything here is wrong or out of date, correct it immediately rather than
just noting it in conversation. This file is the primary context for future
sessions on this project.

## What this is

An autonomous Claude agent, running on the trading server, whose job is to
observe **all** Kalshi markets (every category — politics, sports, economics,
climate, entertainment, crypto, everything), reason about mispricings, and
record **hypothetical** paper trades, iterating on its own strategy over time
via a persistent notes file. **It never places a real trade — see the safety
invariant below, which is a credential fact, not a policy.**

This is a sibling to `baseball-mentions`/`basketball-mentions`/`mlb-outcomes`
but general-purpose (not tied to any one sport or market category).

## Safety invariant — read this before touching kalshi_public.py

**This project must never hold or reference an authenticated Kalshi API key
or private key file.** Do not add `place_order`, `cancel_order`, `_post`, or
any request-signing logic to this codebase, ever — not even behind a
`dry_run` flag.

Why this matters, concretely: Kalshi's market-data endpoints
(`https://external-api.kalshi.com/trade-api/v2` — series, events, markets,
orderbook, settlement results) require **no authentication at all** and cover
every Kalshi market, not just a subset (verified against
`docs.kalshi.com/getting_started/quick_start_market_data`, 2026-07-15: "No
authentication headers are required for the endpoints in this guide").
Because of that, this project simply never needs a Kalshi credential for
anything it does — observing markets, resolving paper-trade settlements, all
of it works unauthenticated. That means there is nothing on this box capable
of authenticating a write to `/portfolio/orders`, so no code the agent writes
— deliberate or accidental — can ever place a real order. This is strictly
stronger than a `dry_run` default, which only guards against the agent
calling the *intended* code path.

`kalshi_public.py` is the only Kalshi client in this repo. It hits
`external-api.kalshi.com`, loads no key, and has no write methods.

## Python version

Unlike the sibling projects, this one has **zero third-party dependencies**
(no `cryptography`, no ML libraries — `kalshi_public.py` is stdlib-only
`urllib`, since there's no request signing to do). So the usual
Anaconda-vs-system-Python trap doesn't apply here: use plain `python3`
everywhere, local and server (confirmed both resolve to a compatible 3.9+
locally and 3.12 on the server; no venv needed on the server for this
project). Still avoid `X | Y` union hints and bare `list[...]`/`dict[...]`
subscripts in case that changes — keep it a habit.

## Paths

**Local repo:** `/Users/Ari/repos/personal/money/kalshi-agent/`
**Server path:** `/root/kalshi-agent/`
**GitHub repo:** private, under the same account as the other sibling
projects — confirm exact URL once created.

## Pipeline

| File | Role |
|------|------|
| `kalshi_public.py` | Unauthenticated read-only Kalshi client — see safety invariant above |
| `fetch_snapshot.py` | Pulls open markets closing within N hours (default 72h), platform-wide, ranked by 24h volume. Archives full raw result to `data/snapshots/<ts>_full.jsonl`, writes curated top-N to `data/snapshots/latest.json` |
| `record_trade.py` | The **only** write path into the ledger — CLI Claude calls via Bash to log one hypothetical trade to `data/paper_trades.csv`, after validating the market is actually open |
| `resolve_trades.py` | Deterministic (no LLM) settlement check + P&L calc for open ledger rows. Run before each agent cycle |
| `telegram.py` | Shared `tg_send()` — same `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` pattern as other sibling projects |
| `telegram_digest.py` | Daily summary: trades opened/closed, win rate, cumulative P&L, latest strategy notes |
| `agent_loop.sh` | One full cycle: `resolve_trades.py` → `fetch_snapshot.py` → `claude -p` (tool-restricted) |
| `strategy_notes.md` | The agent's only persistent memory across cycles — it reads and edits this itself every cycle |
| `reports/build_dashboard_data.py` | Aggregates the ledger into the JSON the Artifact dashboard embeds |
| `reports/kalshi_agent_dashboard.html` | Artifact source — gitignored like `baseball-mentions/reports/`, published on request, not auto-refreshed |

### Ledger schema (`data/paper_trades.csv`)

`trade_id, opened_at, market_ticker, event_ticker, series_ticker, title, side
(yes/no), entry_price, hypothetical_size, rationale, status (open/closed),
closed_at, settlement_result, pnl`

P&L math (in `resolve_trades.py`): `contracts = hypothetical_size /
entry_price`; if the recorded side matches the settlement result, `pnl =
contracts - hypothetical_size` (a $1/contract payout); otherwise `pnl =
-hypothetical_size`.

## Kalshi API quirks discovered building this (not documented anywhere else)

- A resolved market's `status` is `'finalized'`, **not** `'settled'** — the
  `status=settled` query filter works for listing, but the market object
  itself reports `status: 'finalized'` with `result: 'yes'|'no'`.
- `GET /markets/{ticker}/orderbook`'s response key is `orderbook_fp`, not
  `orderbook`. Shape: `{"yes_dollars": [[price_str, size_str], ...],
  "no_dollars": [...]}`.
- A categoryless `GET /series` call returns everything (11,918 series
  observed 2026-07-15) — no need to enumerate categories manually.
- Unauthenticated calls get HTTP 429 after roughly a couple dozen rapid
  requests. `kalshi_public.py`'s `_get` retries with exponential backoff;
  `_get_all_pages` paces itself with a small delay between pages. Respect
  this — it's a shared public resource.
- There are **~9,000+ open markets closing within just the next 3 days**
  platform-wide. A full unbounded scan is both slow and far more than fits
  an LLM's context — `fetch_snapshot.py` bounds by `max_close_ts` and curates
  by 24h volume for exactly this reason. `/markets` accepts `limit` up to
  1000/page (not documented as such in early testing — confirmed via
  `docs.kalshi.com/api-reference/market/get-markets`).
- No `sort`/`order` query param exists on `/markets` — volume-based ranking
  is done client-side in `fetch_snapshot.py`.

## Running a cycle manually

```bash
ssh trading-server
cd /root/kalshi-agent
./agent_loop.sh
```

Logs go to stdout — the systemd service redirects to
`/var/log/kalshi-agent.log` (see below).

## Deployment / autonomy design

The agent runs via the user's existing Claude Code subscription (headless
`claude -p`, authenticated with `CLAUDE_CODE_OAUTH_TOKEN` from a one-time
`claude setup-token` run) — **not** a metered `ANTHROPIC_API_KEY`. Per-cycle
tool access is deliberately narrow: `--allowedTools
"Bash(python record_trade.py:*),Read,Edit(strategy_notes.md)"` — it can log a
paper trade and edit its own notes file, nothing else. `.claude/settings.json`
carries the same allowlist as defense-in-depth for any non-headless
invocation in this directory.

**Cadence:** starts at every 60 minutes (systemd timer, mirroring
`latency-collect.service`'s pattern) — conservative because Claude Pro/Max
subscription rate limits for headless/scripted use aren't precisely
documented. Watch `/var/log/kalshi-agent.log` for auth or rate-limit errors
in the first week and adjust.

**Known gray area:** Anthropic's stated intent for Pro/Max OAuth tokens is
"Claude Code and claude.ai" use. An unattended systemd loop calling `claude
-p` on a schedule is arguably still "the Claude Code CLI," but it's automated/
scheduled invocation — not clearly addressed either way in current docs. If
this trips any enforcement or rate limit, the fallback is a metered
`ANTHROPIC_API_KEY` with an explicit spend cap (would need re-plumbing
`agent_loop.sh` to call the API directly instead of the CLI).

**Verify before trusting the timer:** the exact interaction between
`--allowedTools` and headless mode's tool-denial behavior was researched
but not independently verified end-to-end in this repo before first deploy —
run `./agent_loop.sh` manually at least once and confirm (a) it actually
declines anything outside the allowlist rather than silently proceeding, and
(b) `strategy_notes.md`/`paper_trades.csv` update sensibly, before relying on
the timer unattended.

Systemd units (server-only, not in git — same convention as
`baseball-mentions`):
- `kalshi-agent-cycle.service` + `.timer` — runs `agent_loop.sh` hourly
- `kalshi-agent-digest.service` + `.timer` — runs `telegram_digest.py` daily

## Telegram

Reuses the same bot/chat as `baseball-mentions` (`TELEGRAM_TOKEN`/
`TELEGRAM_CHAT_ID`), messages prefixed `[Kalshi Agent]`. A dedicated bot
(like MLB Outcomes' `@MLB_Outcomes_Bot`) is a trivial swap later if the
shared chat gets noisy.

## Current status

**As of 2026-07-15:** freshly scaffolded, not yet deployed to the server.
Ledger is empty. Next steps: create the GitHub repo, push, deploy to
`/root/kalshi-agent/`, install Claude Code CLI on the server if needed, run
`claude setup-token` (interactive — user must do this step), write and enable
the systemd units, run one manual cycle to verify before enabling the timer.
