#!/usr/bin/env python3
"""
LEGACY — former GDELT tone/volume scorer (schema v1, 10 countries).

Superseded by scripts/import_baselines.py (V-Dem 60% + Freedom House 40%).
A free-news delta updater will replace weekly movement later.

Do not run this against data/scores.json (schema v2).
"""
import sys

print(
    "update_scores.py is legacy and disabled.\n"
    "Use: python scripts/import_baselines.py\n"
    "See METHODOLOGY.md for the current model.",
    file=sys.stderr,
)
sys.exit(2)
