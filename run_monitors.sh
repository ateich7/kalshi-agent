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

# Monitors run with Claude Code credentials stripped and a minimal PATH that
# excludes the `claude` binary. This is a real technical boundary — unlike
# the Write/Edit --disallowedTools entries in agent_loop.sh, which only gate
# Claude Code's own tool calls during a live turn and do nothing to stop a
# script's internal subprocess/file-I/O calls once it's actually executing.
# The thing this specifically guards against: a monitor script (accidentally
# or otherwise) doing `subprocess.run(['claude', '-p', ...])` and silently
# burning the account's Claude usage quota every 2 minutes with nobody
# watching. Without CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY in its
# environment and without `claude` reachable via a bare-name PATH lookup, a
# nested invocation fails immediately instead of authenticating. See
# CLAUDE.md's "Token/usage-limit protection" section — including the
# residual-uncertainty note about credential fallbacks this hasn't been
# tested against yet.
MONITOR_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

shopt -s nullglob
for script in monitors/*.py; do
  name="$(basename "$script" .py)"
  [[ "$name" == _* ]] && continue  # _template.py etc. never auto-run

  {
    echo "=== $(date -u +%FT%TZ) running $script ==="
    env -i "PATH=$MONITOR_PATH" "HOME=$HOME" \
      timeout "$TIMEOUT_SECONDS" python3 "$script"
    status=$?
    if [[ $status -eq 124 ]]; then
      echo "=== TIMED OUT after ${TIMEOUT_SECONDS}s ==="
    elif [[ $status -ne 0 ]]; then
      echo "=== exited $status ==="
    fi
  } >> "$LOG_DIR/$name.log" 2>&1
done
