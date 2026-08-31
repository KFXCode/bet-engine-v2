#!/usr/bin/env python3
"""
picks_log.py — shared record of what the engine published and what happened.

Two files live in the repo and are committed by the workflow:

  picks_log.json    every pick the engine has ever flagged, with the price it
                    was flagged at, and every later price seen for that same
                    side (the line history used for closing-line value).
  results.json      the graded record the board displays.

Nothing here is recomputed after the fact. A pick is recorded the moment it is
published at the price it was published at, so the record cannot drift.
"""

import json
import os

PICKS_PATH = os.environ.get("PICKS_LOG_PATH", "picks_log.json")
RESULTS_PATH = os.environ.get("RESULTS_PATH", "results.json")


# ---- exact conversions. These definitions are fixed; do not "simplify" them ----
def to_decimal(american):
    """+150 -> 2.50 ; -150 -> 1.6667"""
    a = float(american)
    if a == 0:
        raise ValueError("american odds of 0 is not a price")
    return 1.0 + a / 100.0 if a > 0 else 1.0 + 100.0 / abs(a)


def net_odds(american):
    """b — profit per $1 risked on a win."""
    return to_decimal(american) - 1.0


def implied(american):
    """The probability the price is charging for."""
    return 1.0 / to_decimal(american)


def edge_points(p, american):
    """(our probability - the price's probability), in percentage points."""
    return (p - implied(american)) * 100.0


def expected_value(p, american):
    """EV per $1 risked = p*b - (1-p)."""
    b = net_odds(american)
    return p * b - (1.0 - p)


def no_vig(american_side, american_other):
    """Side's share of the two prices once the book's margin is divided out.

    Both sides' implied probabilities sum to more than 1; the excess is the
    hold. Dividing each by the sum is the market's own estimate with the
    margin removed, which is what closing-line value must be measured against
    (comparing raw prices measures the book's margin, not your timing).
    """
    if american_other is None:
        return None
    a, b = implied(american_side), implied(american_other)
    total = a + b
    return a / total if total > 0 else None


def clv_points(bet_price, bet_other, close_price, close_other):
    """Closing-line value in probability points, no-vig where possible.

    Positive means the price you took was better than where the market closed.
    """
    bf, cf = no_vig(bet_price, bet_other), no_vig(close_price, close_other)
    if bf is not None and cf is not None:
        return (cf - bf) * 100.0
    # fall back to raw prices when only one side is known
    return (implied(close_price) - implied(bet_price)) * 100.0


# ------------------------------------------------------------------ storage ----
def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_picks():
    d = load(PICKS_PATH, {})
    if not isinstance(d, dict) or not isinstance(d.get("picks"), list):
        return {"picks": []}
    return d


def save_picks(d):
    with open(PICKS_PATH, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)


def save_results(d):
    with open(RESULTS_PATH, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)


def pick_id(sport, away, home, market, side):
    return "|".join([sport, away + " @ " + home, market, side])
