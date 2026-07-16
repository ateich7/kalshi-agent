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
| `monitors/` | Agent-authored scripts that run **unsupervised, between hourly cycles** — see "Expanded autonomy" section below |
| `run_monitors.sh` | The only thing that executes `monitors/*.py` — human-authored, protected from agent Write/Edit, runs on a short cadence via `kalshi-agent-monitors.timer` |
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
tool access is broad by design (see "Expanded autonomy" below):
`--allowedTools "Read,WebSearch,Write,Edit,Bash(python3 *.py:*)"` paired with
`--disallowedTools` covering a short list of protected files (ledger
integrity + the agent's own harness). `.claude/settings.json` carries the
same allow/deny shape as defense-in-depth for any non-headless invocation in
this directory.

**WebSearch was added 2026-07-16**, after the first live cycle showed the
agent correctly declining to trade on most markets because it had no
independent read on live facts (BTC/ETH spot price, current weather,
contestant news) — without it, `implied_prob` from the snapshot is the only
signal, so "mispriced" collapses to "disagrees with the crowd," not a real
edge. Known tradeoff, accepted deliberately: web content is untrusted
input to an autonomous loop, so this opens a prompt-injection surface that
didn't exist before (a fetched page could contain text aimed at the agent).
Mitigated by keeping the write surface unchanged — WebSearch only feeds
reasoning, it doesn't unlock any new write action — and by an explicit
system-prompt instruction to treat search results as data, never
instructions.

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

**Verified 2026-07-16:** ran `./agent_loop.sh` manually three times while
debugging first deploy. Findings, in order: (1) an untrusted-workspace state
in `/root/.claude.json` made headless mode ignore `.claude/settings.json` and
stall waiting for interactive approval it could never get — fixed by running
`claude` interactively once in `/root/kalshi-agent` and accepting the trust
dialog (`hasTrustDialogAccepted` in `/root/.claude.json`); (2) the agent
reached for Bash (not allowlisted) to inspect the 300-market pretty-printed
snapshot instead of just using Read — fixed by trimming the curated snapshot
to 12 relevant fields, compact (non-indented) JSON, and top-n 150, plus an
explicit prompt instruction that Read alone is sufficient; (3) with both
fixed, a full cycle ran cleanly end to end and produced a sensible
`strategy_notes.md` entry. Timers enabled after this verification.

Systemd units (server-only, not in git — same convention as
`baseball-mentions`):
- `kalshi-agent-cycle.service` + `.timer` — runs `agent_loop.sh` hourly
- `kalshi-agent-digest.service` + `.timer` — runs `telegram_digest.py` daily
- `kalshi-agent-monitors.service` + `.timer` — runs `run_monitors.sh` every 2
  minutes (see "Monitors" below; added 2026-07-16, not yet deployed to the
  server as of this writing — needs manual `systemctl enable --now` like the
  other two units did)

## Expanded autonomy — general Write/Edit/Bash(python3), 2026-07-16

**This was a deliberate, explicit user decision to widen scope beyond an
initial narrower design** (a `monitors/*.py`-only write scope was built
first and rejected as too restrictive the same day — see git history on
`agent_loop.sh`/`CLAUDE.md` around 2026-07-16 if you need the earlier
version's reasoning). The agent can now Write/Edit anywhere in this repo and
run any Python file it writes via `Bash(python3 *.py:*)`, so it can build
real tooling across cycles — helper modules, indicator libraries, backtests
— not just record one-off trades. `monitors/` remains the designated
location for scripts meant to run **unsupervised between hourly cycles**:
`run_monitors.sh` executes every `monitors/*.py` file on a 2-minute cadence
(`kalshi-agent-monitors.timer`), independent of `agent_loop.sh`, so
time-sensitive conditions (a strike ladder crossing a threshold, a countdown
to a known settlement time) don't have to wait up to 60 minutes.

**The honest technical framing, worth restating precisely:** once an agent
can write and execute arbitrary Python, there is no meaningful sandbox left
to describe — stdlib alone (`subprocess`, `os`, `socket`, `shutil`) can do
anything a raw shell command can. A directory-scoped write path (the
original `monitors/`-only design) was never a real security boundary once
code execution is in play; it was organizational tidiness at best. This
section exists so nobody down the line mistakes the remaining guardrails
for more than they are.

**What's still protected via `--disallowedTools`/`deny`, and the important
caveat on what that actually buys:**
- `kalshi_public.py`, `record_trade.py`, `resolve_trades.py`,
  `data/paper_trades.csv`, plus `agent_loop.sh`, `run_monitors.sh`,
  `.claude/settings.json`, `CLAUDE.md` are all off-limits to the agent's own
  Edit/Write tool calls during a live turn.
- **The caveat, stated precisely because it's easy to overclaim:** this
  only stops Claude Code's Edit/Write *tool* from targeting those paths
  directly. It does **not** stop a script the agent writes elsewhere (e.g.
  `monitors/foo.py`) from opening `data/paper_trades.csv` in plain Python
  and appending a row, once `Bash(python3 *.py:*)` actually executes that
  script — the tool-permission system has no visibility into what a
  script's own file I/O does. So the ledger-integrity list above is a real
  guard against the agent directly editing those files with its tools, but
  it is **not** a guarantee against a self-authored script achieving the
  same effect indirectly. The actual backstop for that is convention/
  instruction (see `monitors/README.md` and the cycle prompt: "log a trade
  via `record_trade.py`, never write the CSV directly") plus the fact that
  there's no incentive for the agent to bypass its own validated write path
  — but don't mistake the `--disallowedTools` list for an airtight technical
  boundary once arbitrary Python execution is in scope. It's the same
  caveat as "Bash(python3) already means no directory scoping is real,"
  applied one level deeper.
- OS-level scheduling (cron/systemd) is still **not** granted to the agent
  itself — `run_monitors.sh` is a human-installed, fixed-cadence runner.
  This one *is* a real boundary (it's an access grant, not a file-content
  question) and is a deliberate orthogonal one: giving an LLM direct
  `crontab`/`systemctl` access is a server-administration risk, not a
  trading-sophistication one, and costs it nothing creative to not have it.

**A new consideration this expansion actually introduces (financial risk is
unaffected — the Kalshi safety invariant is absence-of-credential, not a
permission, so it holds regardless of write/exec scope):** this environment
holds a live `TELEGRAM_TOKEN` (see `telegram.py`/Telegram section below).
Broad code execution means the agent *could* use it to message the user's
real phone at will — a genuine real-world side effect, unlike everything
else here which stays inside the ledger/filesystem sandbox. This is **not**
structurally blocked (denying Read on `telegram.py` wouldn't meaningfully
stop a determined script from hitting the Telegram Bot API directly, since
the token is just inherited from the environment by any Python process) —
it's handled by an explicit prompt-level instruction only: never send a
Telegram message outside the existing daily-digest path. Watch
`/var/log/kalshi-agent.log` and the Telegram chat itself for any violation
of this in the first weeks after deploy.

**Not yet deployed as of 2026-07-16:** the expanded allowlist and
`monitors/`/`run_monitors.sh` scaffold are pushed to GitHub (`main`) but not
yet pulled onto the server, and `kalshi-agent-monitors.timer` hasn't been
installed/enabled — needs the same one-time `systemctl enable --now` step
the other two units got, plus a fresh round of manual verification runs
(same pattern as the original three-manual-runs verification for
`agent_loop.sh`) given how much more latitude this grants versus what was
actually verified live so far. Specifically verify the credential-scrubbing
in `run_monitors.sh` (below) actually prevents a nested `claude` invocation
from authenticating before trusting the 2-minute timer unattended.

## Token/usage-limit protection

**Added 2026-07-16, same session as "Expanded autonomy" above.** Granting
`Bash(python3 *.py:*)` means any script the agent writes can, in principle,
`subprocess.run(['claude', '-p', ...])` and spawn another full headless
session. That's a materially different risk than a bad paper trade or a
runaway file write: this project's entire cadence assumes **exactly one**
`claude -p` invocation per hourly cycle (see "Known gray area" above —
Claude Pro/Max subscription limits for headless/scripted use aren't
precisely documented, so staying well inside them matters). A script that
recursively invokes `claude` — especially one firing every 2 minutes via
`run_monitors.sh` with nobody watching in real time — could burn through
the account's usage quota fast and silently.

Two mitigations, matched to where each is actually enforceable:

- **`run_monitors.sh` strips Claude Code credentials before running any
  monitor script** (`env -i PATH=... HOME=...`) — clears
  `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` and restricts `PATH` to
  exclude wherever the `claude` binary lives. This is a real technical
  boundary, not a convention: even if a monitor script tried to shell out
  to `claude`, it has nothing to authenticate with and can't resolve the
  binary via a bare-name lookup.
- **The live hourly turn can't be environment-scrubbed the same way** — it
  needs `CLAUDE_CODE_OAUTH_TOKEN` in its own environment to run at all, and
  any subprocess it spawns inherits that by default. This is handled by an
  explicit, hard prompt-level instruction only (in `agent_loop.sh`'s
  `PROMPT` and `--append-system-prompt`): never invoke `claude` yourself,
  from Bash or from any script you write. Watch `/var/log/kalshi-agent.log`
  and `ps` output for any unexpected `claude` process spawned outside the
  scheduled hourly/daily units as a sign this was violated.
- **`agent_loop.sh` takes an `flock` lock** (`/tmp/kalshi-agent-cycle.lock`)
  so an overlapping manual run and timer fire can't double-invoke `claude`
  within the same hour — the second invocation exits immediately instead of
  running concurrently.

**Residual uncertainty, stated plainly:** whether `claude -p` can
authenticate from some on-disk cached credential independent of
`CLAUDE_CODE_OAUTH_TOKEN` (e.g. a session file written by the one-time
`claude setup-token` run) isn't something this repo can verify without
testing directly on the server. Env-scrubbing closes the most likely path;
it is not proven airtight — confirm it before trusting the 2-minute timer
unattended (e.g. have a monitor script attempt
`subprocess.run(['claude','--version'])` and check it actually fails under
`run_monitors.sh`'s stripped environment).

## Telegram

Reuses the same bot/chat as `baseball-mentions` (`TELEGRAM_TOKEN`/
`TELEGRAM_CHAT_ID`), messages prefixed `[Kalshi Agent]`. A dedicated bot
(like MLB Outcomes' `@MLB_Outcomes_Bot`) is a trivial swap later if the
shared chat gets noisy.

## Current status

**As of 2026-07-16:** deployed and live. `kalshi-agent-cycle.timer` (hourly)
and `kalshi-agent-digest.timer` (daily, 5pm America/New_York — DST-safe via
an explicit timezone in `OnCalendar`, not a hardcoded UTC offset) both
enabled. First real
cycle logged zero trades (correctly — see WebSearch note above for why that
changed) and one strategy-notes entry. Dashboard not yet published as an
Artifact — ask for it once there's enough ledger history to be worth a look.
