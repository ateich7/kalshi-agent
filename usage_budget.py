#!/usr/bin/env python3
"""
Claude-subscription usage budget for this project's `claude -p` calls.

The account's usage limits (a rolling 5-hour "session" window and a 7-day
weekly window) are shared between this headless loop and the operator's own
interactive Claude use. This module keeps the agent to a fixed share of both:

  - WEEKLY_SHARE (default 33 pct-points of the 7-day limit) per weekly window
  - FIVE_HOUR_SHARE (default 33 pct-points of the 5-hour limit) per session
    window

and paces the weekly share across the days left in the window, so the agent
can't burn the whole week on day one and sit idle for six days.

How it measures spend: the account's live utilization comes from the same
endpoint Claude Code's own /usage screen reads (api/oauth/usage). Around each
`claude -p` call we snapshot utilization before and after; the delta is
debited to the agent. Anything the operator does concurrently during a cycle
is also debited to the agent -- deliberately conservative.

Daily plan: at the first check of each America/New_York day,
    day_budget = remaining_weekly_share / days_left_in_window
Unspent budget rolls forward automatically (tomorrow's remaining is larger);
an overrun shrinks every later day. Within a day, cycles are spread out:
a cycle may only start if today's spend is at or below the pro-rata share of
the day elapsed so far.

Fails CLOSED: if utilization can't be read, no cycle runs.

Commands (all print no secrets):
  status                 human-readable budget report
  gate --kind K          exit 0 if a K run (cycle|digest) may start now
  begin --kind K         snapshot utilization before a run
  end                    snapshot after, debit the delta
  watch --pid PID        poll during a run; SIGTERM PID if a cap is breached
  prompt-context         a short budget summary for the agent's prompt

Harness-protected: the agent can't Edit this file or its state file (see
agent_loop.sh --disallowedTools).
"""

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo('America/New_York')
except Exception:  # pragma: no cover -- tzdata missing; fall back to UTC days
    LOCAL_TZ = timezone.utc

ROOT = Path(__file__).parent
STATE_PATH = ROOT / 'data' / 'usage_budget_state.json'

WEEKLY_SHARE = float(os.environ.get('KALSHI_WEEKLY_SHARE', 33))
FIVE_HOUR_SHARE = float(os.environ.get('KALSHI_FIVE_HOUR_SHARE', 33))
# Never start (and kill an in-flight run) when the ACCOUNT as a whole is this
# close to a limit -- the operator is using it heavily and the agent yields.
ACCOUNT_7D_CEILING = float(os.environ.get('KALSHI_ACCOUNT_7D_CEILING', 90))
ACCOUNT_5H_CEILING = float(os.environ.get('KALSHI_ACCOUNT_5H_CEILING', 80))
# Starting guess for one research cycle's cost, in pct-points, until real
# measurements replace it (EMA below).
DEFAULT_CYCLE_COST_7D = 1.5
DEFAULT_CYCLE_COST_5H = 8.0
EMA_ALPHA = 0.3
WATCH_INTERVAL_S = 90

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
MESSAGES_URL = 'https://api.anthropic.com/v1/messages'


# ---------------------------------------------------------------- utilization

def _token():
    tok = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
    if tok:
        return tok
    cred = Path.home() / '.claude' / '.credentials.json'
    if cred.exists():
        try:
            return json.loads(cred.read_text())['claudeAiOauth']['accessToken']
        except Exception:
            pass
    raise RuntimeError('no Claude OAuth token in env or ~/.claude/.credentials.json')


def _parse_iso(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def _from_usage_endpoint(tok):
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': 'Bearer ' + tok,
        'anthropic-beta': 'oauth-2025-04-20',
        'Content-Type': 'application/json',
        'User-Agent': 'kalshi-agent-usage-budget/1.0',
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())

    def window(key):
        w = d.get(key) or {}
        return {'util': float(w.get('utilization') or 0.0),
                'resets_at': _parse_iso(w.get('resets_at'))}
    return {'five_hour': window('five_hour'), 'seven_day': window('seven_day'),
            'source': 'oauth/usage'}


def _from_ratelimit_headers(tok, model):
    """A 1-token Haiku call returns the account's unified rate-limit headers
    (same 5h/7d windows /usage shows). Costs a negligible sliver of quota.
    This is the primary source: the long-lived `claude setup-token` token
    lacks the user:profile scope api/oauth/usage requires (403, verified
    2026-10-14)."""
    body = json.dumps({'model': model, 'max_tokens': 1,
                       'messages': [{'role': 'user', 'content': '.'}]}).encode()
    req = urllib.request.Request(MESSAGES_URL, data=body, headers={
        'Authorization': 'Bearer ' + tok,
        'anthropic-beta': 'oauth-2025-04-20',
        'anthropic-version': '2023-06-01',
        'Content-Type': 'application/json',
    })
    try:
        r = urllib.request.urlopen(req, timeout=30)
        h = r.headers
    except urllib.error.HTTPError as e:
        h = e.headers  # a limit-hit 429 still carries the headers we want
    def window(tag):
        u = h.get('anthropic-ratelimit-unified-%s-utilization' % tag)
        reset = h.get('anthropic-ratelimit-unified-%s-reset' % tag)
        if u is None:
            raise RuntimeError('rate-limit headers missing (%s)' % tag)
        return {'util': float(u) * 100.0,
                'resets_at': datetime.fromtimestamp(int(reset), timezone.utc) if reset else None}
    return {'five_hour': window('5h'), 'seven_day': window('7d'),
            'source': 'ratelimit-headers'}


PROBE_MODELS = ('claude-haiku-4-5', 'claude-haiku-5-5')


def fetch_utilization():
    tok = _token()
    try:
        return _from_usage_endpoint(tok)
    except Exception:
        pass  # expected with a setup-token (403 scope / 429); use headers
    err = None
    for m in PROBE_MODELS:
        try:
            return _from_ratelimit_headers(tok, m)
        except RuntimeError as e:
            err = e
    raise err


# ---------------------------------------------------------------------- state

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, indent=2, default=str))
    tmp.replace(STATE_PATH)


def _window_key(resets_at):
    # Round to the hour: resets_at can jitter by seconds between reads.
    if resets_at is None:
        return None
    return resets_at.replace(minute=0, second=0, microsecond=0).isoformat()


def _now():
    return datetime.now(timezone.utc)


def sync_windows(st, u, now=None):
    """Roll the week/5h/day accumulators when their windows turn over, and
    fix today's budget at the first check of each local day."""
    now = now or _now()
    wk = _window_key(u['seven_day']['resets_at'])
    week = st.get('week') or {}
    if week.get('key') != wk:
        # New weekly window (or first run): start the share fresh. A window
        # with no resets_at yet (nothing used) gets keyed on first usage.
        st['week'] = {'key': wk, 'agent_spent': 0.0}
        st.pop('day', None)

    fk = _window_key(u['five_hour']['resets_at'])
    fh = st.get('five_hour') or {}
    if fh.get('key') != fk:
        st['five_hour'] = {'key': fk, 'agent_spent': 0.0}

    today = now.astimezone(LOCAL_TZ).date().isoformat()
    day = st.get('day') or {}
    if day.get('date') != today:
        remaining = max(0.0, WEEKLY_SHARE - st['week']['agent_spent'])
        reset = u['seven_day']['resets_at'] or (now + timedelta(days=7))
        start_of_day = now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        days_left = max(1.0, (reset - start_of_day).total_seconds() / 86400.0)
        st['day'] = {'date': today, 'budget': remaining / days_left,
                     'days_left': round(days_left, 2), 'spent': 0.0}
    return st


def _est(st):
    return (st.get('cycle_cost_7d') or DEFAULT_CYCLE_COST_7D,
            st.get('cycle_cost_5h') or DEFAULT_CYCLE_COST_5H)


def _day_fraction_elapsed(now=None):
    local = (now or _now()).astimezone(LOCAL_TZ)
    return (local.hour * 3600 + local.minute * 60 + local.second) / 86400.0


# ----------------------------------------------------------------- decisions

def gate(kind, st, u, now=None):
    """Return (allowed, reason)."""
    now = now or _now()
    acct7, acct5 = u['seven_day']['util'], u['five_hour']['util']
    if kind == 'cycle':
        # Don't start a cycle that would itself push the account over.
        c7, c5 = _est(st)
        acct7, acct5 = acct7 + c7, acct5 + c5
    if acct7 >= ACCOUNT_7D_CEILING:
        return False, 'account weekly usage would reach %.0f%% (>= %.0f%% ceiling) -- yielding to operator' % (acct7, ACCOUNT_7D_CEILING)
    if acct5 >= ACCOUNT_5H_CEILING:
        return False, 'account 5h session usage would reach %.0f%% (>= %.0f%% ceiling) -- yielding to operator' % (acct5, ACCOUNT_5H_CEILING)

    week_left = WEEKLY_SHARE - st['week']['agent_spent']
    fh_left = FIVE_HOUR_SHARE - st['five_hour']['agent_spent']
    if kind == 'digest':
        # Tiny zero-tool call; only blocked once a share is fully spent.
        if week_left <= 0:
            return False, 'weekly agent share exhausted'
        if fh_left <= 0:
            return False, '5h agent share exhausted'
        return True, 'ok'

    c7, c5 = _est(st)
    if c7 > week_left:
        return False, 'weekly share: %.1f of %.0f pts left, est cycle %.1f' % (week_left, WEEKLY_SHARE, c7)
    if c5 > fh_left:
        return False, '5h share: %.1f of %.0f pts left, est cycle %.1f' % (fh_left, FIVE_HOUR_SHARE, c5)
    day = st['day']
    if day['spent'] + c7 > day['budget'] + 1e-9:
        return False, "today's budget: spent %.2f of %.2f pts, est cycle %.2f" % (day['spent'], day['budget'], c7)
    pace = day['budget'] * _day_fraction_elapsed(now)
    if day['spent'] > pace + 1e-9:
        return False, 'pacing: spent %.2f, pro-rata allowance so far %.2f' % (day['spent'], pace)
    return True, 'ok'


def debit(st, before, after, kind, now=None):
    d7 = max(0.0, after['seven_day']['util'] - before['seven_day']['util'])
    d5 = max(0.0, after['five_hour']['util'] - before['five_hour']['util'])
    # If a window rolled over mid-run, everything now in the new window is
    # (conservatively) the agent's.
    if _window_key(after['seven_day']['resets_at']) != _window_key(before['seven_day']['resets_at']):
        d7 = after['seven_day']['util']
    if _window_key(after['five_hour']['resets_at']) != _window_key(before['five_hour']['resets_at']):
        d5 = after['five_hour']['util']
    sync_windows(st, after, now)
    st['week']['agent_spent'] += d7
    st['five_hour']['agent_spent'] += d5
    st['day']['spent'] += d7
    if kind == 'cycle':
        c7, c5 = _est(st)
        st['cycle_cost_7d'] = round((1 - EMA_ALPHA) * c7 + EMA_ALPHA * d7, 3) if 'cycle_cost_7d' in st else max(d7, 0.5)
        st['cycle_cost_5h'] = round((1 - EMA_ALPHA) * c5 + EMA_ALPHA * d5, 3) if 'cycle_cost_5h' in st else max(d5, 2.0)
    return d7, d5


def _ser(u):
    return {k: ({'util': v['util'], 'resets_at': v['resets_at'].isoformat() if v['resets_at'] else None}
                if isinstance(v, dict) else v) for k, v in u.items()}


def _de(u):
    return {k: ({'util': v['util'], 'resets_at': _parse_iso(v['resets_at'])}
                if isinstance(v, dict) else v) for k, v in u.items()}


def _fmt_reset(dt):
    if not dt:
        return 'n/a'
    return dt.astimezone(LOCAL_TZ).strftime('%a %b %d %I:%M%p ET')


def summary_lines(st, u):
    c7, c5 = _est(st)
    day = st['day']
    return [
        'Account usage now: 5h session %.0f%% (resets %s), weekly %.0f%% (resets %s)  [via %s]' % (
            u['five_hour']['util'], _fmt_reset(u['five_hour']['resets_at']),
            u['seven_day']['util'], _fmt_reset(u['seven_day']['resets_at']), u.get('source')),
        'Agent weekly share: %.1f / %.0f pts used (%.1f left, %.1f days left in window)' % (
            st['week']['agent_spent'], WEEKLY_SHARE, WEEKLY_SHARE - st['week']['agent_spent'], day['days_left']),
        'Agent 5h share: %.1f / %.0f pts used this session window' % (st['five_hour']['agent_spent'], FIVE_HOUR_SHARE),
        "Today's plan: %.2f / %.2f pts spent; est per cycle %.2f weekly pts, %.1f 5h pts (~%d cycles/day)" % (
            day['spent'], day['budget'], c7, c5, int(day['budget'] // c7) if c7 else 0),
    ]


# ---------------------------------------------------------------------- CLI

def _ctx():
    st = load_state()
    u = fetch_utilization()
    sync_windows(st, u)
    return st, u


def cmd_status(_):
    st, u = _ctx()
    save_state(st)
    print('\n'.join(summary_lines(st, u)))
    ok, why = gate('cycle', st, u)
    print('Next research cycle: %s' % ('ALLOWED' if ok else 'BLOCKED -- ' + why))


def cmd_gate(a):
    try:
        st, u = _ctx()
    except Exception as e:
        print('usage_budget: cannot read utilization (%s) -- failing closed' % e)
        return 1
    save_state(st)
    ok, why = gate(a.kind, st, u)
    print('usage_budget: %s %s -- %s' % (a.kind, 'allowed' if ok else 'BLOCKED', why))
    print('usage_budget: ' + ' | '.join(summary_lines(st, u)))
    return 0 if ok else 3


def cmd_begin(a):
    st, u = _ctx()
    st['inflight'] = {'kind': a.kind, 'started_at': _now().isoformat(), 'before': _ser(u)}
    save_state(st)
    return 0


def cmd_end(_):
    st = load_state()
    inf = st.pop('inflight', None)
    if not inf:
        print('usage_budget: end called with nothing in flight')
        return 0
    after = fetch_utilization()
    sync_windows(st, after)
    d7, d5 = debit(st, _de(inf['before']), after, inf['kind'])
    hist = st.setdefault('history', [])
    hist.append({'kind': inf['kind'], 'started_at': inf['started_at'], 'ended_at': _now().isoformat(),
                 'weekly_pts': round(d7, 2), 'five_hour_pts': round(d5, 2)})
    st['history'] = hist[-60:]
    save_state(st)
    print('usage_budget: %s used %.2f weekly pts, %.2f 5h pts' % (inf['kind'], d7, d5))
    print('usage_budget: ' + ' | '.join(summary_lines(st, after)))
    return 0


def cmd_watch(a):
    """Kill the run if it blows through a hard cap mid-flight. Day overrun is
    allowed up to one estimated cycle (later days absorb it); weekly/5h
    shares and account ceilings are hard."""
    while True:
        time.sleep(WATCH_INTERVAL_S)
        try:
            os.kill(a.pid, 0)
        except OSError:
            return 0
        st = load_state()
        inf = st.get('inflight')
        if not inf:
            return 0
        try:
            now_u = fetch_utilization()
        except Exception as e:
            print('usage_budget watch: read failed (%s), continuing' % e, flush=True)
            continue
        before = _de(inf['before'])
        d7 = max(0.0, now_u['seven_day']['util'] - before['seven_day']['util'])
        d5 = max(0.0, now_u['five_hour']['util'] - before['five_hour']['util'])
        c7, _ = _est(st)
        reasons = []
        if st['week']['agent_spent'] + d7 > WEEKLY_SHARE:
            reasons.append('weekly share')
        if st['five_hour']['agent_spent'] + d5 > FIVE_HOUR_SHARE:
            reasons.append('5h share')
        if st['day']['spent'] + d7 > st['day']['budget'] + c7:
            reasons.append("today's budget (+1 cycle slack)")
        if now_u['seven_day']['util'] >= ACCOUNT_7D_CEILING:
            reasons.append('account weekly ceiling')
        if now_u['five_hour']['util'] >= ACCOUNT_5H_CEILING:
            reasons.append('account 5h ceiling')
        if reasons:
            print('usage_budget watch: cap breached (%s) at +%.1f weekly / +%.1f 5h pts -- stopping run' % (
                ', '.join(reasons), d7, d5), flush=True)
            try:
                os.kill(a.pid, signal.SIGTERM)
            except OSError:
                pass
            return 0


def cmd_prompt_context(_):
    st, u = _ctx()
    save_state(st)
    c7, _ = _est(st)
    day = st['day']
    print('\n'.join([
        'USAGE BUDGET (read this -- it is a hard constraint, enforced by the harness):',
        "This loop may use at most %.0f%% of the operator's weekly Claude limit and %.0f%% of any"
        ' 5-hour session window; the rest belongs to the operator. A watchdog will kill this cycle'
        ' mid-task if it breaches a cap, so unfinished work (notes, trades) would be lost.' % (WEEKLY_SHARE, FIVE_HOUR_SHARE),
    ] + summary_lines(st, u) + [
        'Plan your effort to fit roughly one cycle (~%.2f weekly pts). Prioritize: evaluate the'
        ' snapshot, record any trades, and write your strategy_notes.md entry EARLY rather than last;'
        ' spend leftover turns on tooling/exploration only after that. Skip open-ended research.'
        ' You can run `python3 usage_budget.py status` to re-check (read-only).' % c7,
    ]))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    g = sub.add_parser('gate'); g.add_argument('--kind', choices=['cycle', 'digest'], default='cycle')
    b = sub.add_parser('begin'); b.add_argument('--kind', choices=['cycle', 'digest'], default='cycle')
    sub.add_parser('end')
    w = sub.add_parser('watch'); w.add_argument('--pid', type=int, required=True)
    sub.add_parser('prompt-context')
    a = p.parse_args()
    fn = {'status': cmd_status, 'gate': cmd_gate, 'begin': cmd_begin, 'end': cmd_end,
          'watch': cmd_watch, 'prompt-context': cmd_prompt_context}[a.cmd]
    sys.exit(fn(a) or 0)


if __name__ == '__main__':
    main()
