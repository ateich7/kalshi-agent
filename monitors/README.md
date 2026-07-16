# monitors/

Scripts here run **unsupervised, independent of the hourly agent cycle** —
`run_monitors.sh` executes every `monitors/*.py` file on a short cadence
(`kalshi-agent-monitors.timer`, every 2 minutes) so time-sensitive setups
don't have to wait up to 60 minutes for the next `agent_loop.sh` run. This is
the designated place for anything you want to keep running between hourly
cycles — the agent has broad write access across this whole repo now (see
CLAUDE.md's "Expanded autonomy" section), but `run_monitors.sh` only ever
executes files in this specific directory, so this is where scheduled logic
belongs; everything else (helper modules, backtests, indicator libraries,
scratch analysis) can live wherever makes sense in the repo and gets pulled
in during your own hourly reasoning pass instead.

## A few things worth doing, not enforced but load-bearing

- **Reuse `kalshi_public.py` for any Kalshi call.** It has the rate-limit
  backoff/pacing this whole project depends on — a raw request here risks
  tripping the shared 429 threshold for every other monitor and the hourly
  cycle too. Nothing stops you from rolling your own; it's just a bad idea.
- **Log a trade via `record_trade.py`, not a direct CSV write.** You don't
  have Write/Edit access to `record_trade.py` or `data/paper_trades.csv`
  themselves (see CLAUDE.md) — that's the one hard boundary in this repo,
  because it's what keeps the ledger's P&L trustworthy as a signal of
  whether your calls are actually good.
- **Exit within ~60s.** `run_monitors.sh` gives each script `timeout 60s`
  and kills anything still running past that — write poll-once-and-exit
  scripts, not daemons, and let the runner's own cadence call you again.
- **Log with print()** — stdout/stderr are captured to
  `data/monitor_logs/<script_name>.log` (appended every run), which is how
  you'll know what happened next time you wake up.

Filenames starting with `_` (e.g. `_template.py`) are never auto-executed by
`run_monitors.sh` — use that prefix for shared helpers or templates. To
retire a monitor, rewrite it into a no-op stub, or delete it directly (you
have general Write/Edit access now, unlike the earlier version of this
file).
