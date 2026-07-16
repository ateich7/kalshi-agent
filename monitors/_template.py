#!/usr/bin/env python3
"""
Template for a monitors/ script. Copy this, rename without the leading `_`,
and fill in real logic. Read monitors/README.md for the full contract this
must follow before run_monitors.sh will execute it unsupervised.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from kalshi_public import KalshiPublicClient  # noqa: E402


def main():
    k = KalshiPublicClient()

    # Example: check one specific market for a condition worth acting on.
    # market = k.get_market('SOME-TICKER')
    # if <condition>:
    #     subprocess.run([
    #         'python3', str(REPO_ROOT / 'record_trade.py'),
    #         '--ticker', 'SOME-TICKER', '--side', 'yes', '--price', '0.50',
    #         '--size', '10', '--rationale', 'why this monitor fired',
    #     ], check=True)

    print('checked, no action')


if __name__ == '__main__':
    main()
