#!/bin/bash
# Executes every agent-authored script in monitors/ on a short, independent
# cadence (kalshi-agent-monitors.timer) so time-sensitive setups don't have
# to wait for the next hourly agent_loop.sh cycle. This is the ONLY thing
# that ever executes monitor code — the agent can write to monitors/ but has
# no cron/systemd access itself, so this script is the sole trusted boundary
# between "agent writes a script" and "script actually runs unsupervised."
# Keep it dumb and defensive; all the intelligence lives in the scripts it
# runs, not here.
set -uo pipefail  # deliberately not -e: one monitor's failure must not stop the rest

cd "$(dirname "${BASH_SOURCE[0]}")"

TIMEOUT_SECONDS=60
LOG_DIR="data/monitor_logs"
mkdir -p "$LOG_DIR"

shopt -s nullglob
for script in monitors/*.py; do
  name="$(basename "$script" .py)"
  [[ "$name" == _* ]] && continue  # _template.py etc. never auto-run

  {
    echo "=== $(date -u +%FT%TZ) running $script ==="
    timeout "$TIMEOUT_SECONDS" python3 "$script"
    status=$?
    if [[ $status -eq 124 ]]; then
      echo "=== TIMED OUT after ${TIMEOUT_SECONDS}s ==="
    elif [[ $status -ne 0 ]]; then
      echo "=== exited $status ==="
    fi
  } >> "$LOG_DIR/$name.log" 2>&1
done
