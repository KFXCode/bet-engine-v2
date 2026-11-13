"""
sharpen.py -- turns off the kinds of picks that have lost money (Nov 13, 2026).

Every rule here comes from V2's own graded picks (picks_log.json, Sep-Nov 2026).
Nothing is deleted from the feed: a blocked side is listed in the game's
"block" list, and the board (index.html) and the grader (grade.py) both skip
anything listed there, so they always agree on what was published.

  SIDES (game lines)                                   record when bet
  - Moneyline favorites -101 to -200, every sport      49-59, -23.7 units
  - College football moneyline favorites -201 or more  67-31 but -11.4 units
  - NBA moneyline underdogs                            13-36, -9.5 units
  - NFL moneyline underdogs +100 to +150               3-11, -7.2 units
  - College football small favorites -0.5 to -7.5      21-33 since October
  - College football underdogs +8 or more              37-41, losing
  - College football favorites -8 or more: only with
    8-12 points of edge (14-6, +6.8 units with it)
  - NFL and NBA spread favorites                       29-43, -16.4 units
  PROPS
  - NFL passing yards (over and under)                 12-22, -11.4 units
  - NFL rushing / receiving yards OVERS                44-48, -9.1 units
  - NBA points, rebounds and pts+reb+ast OVERS         56-67, -18.0 units
  (Unders on the same markets win: 200-147, +27.3 units. They stay.)
"""

import math

CFB_FAV_MIN_EDGE = 8.0
CFB_FAV_MAX_EDGE = 12.0

BLOCKED_PROPS = {
    ("NFL", "Passing yards", "Over"), ("NFL", "Passing yards", "Under"),
    ("NFL", "Rushing yards", "Over"), ("NFL", "Receiving yards", "Over"),
    ("NBA", "Points", "Over"), ("NBA", "Rebounds", "Over"), ("NBA", "Pts+Reb+Ast", "Over"),
}


def _implied(american):
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def _ncdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ml_blocked(sport, price):
    if price is None:
        return False
    p = float(price)
    if -200 <= p <= -101:
        return True
    if sport == "NCAAF" and p <= -201:
        return True
    if sport == "NBA" and p >= 100:
        return True
    if sport == "NFL" and 100 <= p <= 150:
        return True
    return False


def _spread_blocked(g, line, p_cover, price):
    sport = g.get("sport")
    if sport in ("NFL", "NBA") and line < 0:
        return True
    if sport == "NCAAF":
        if -7.5 <= line <= -0.5 or line >= 8:
            return True
        if line <= -8:
            if price is None:
                return True
            edge = (p_cover - _implied(price)) * 100.0
            return not (CFB_FAV_MIN_EDGE <= edge <= CFB_FAV_MAX_EDGE)
    return False


def block_list(g):
    """Sides of one game that are not bet, as 'Market:selection' strings."""
    out = []
    sport = g.get("sport")
    if _ml_blocked(sport, g.get("mlHome")):
        out.append("Moneyline:home")
    if _ml_blocked(sport, g.get("mlAway")):
        out.append("Moneyline:away")
    sd, margin, spread = g.get("sdMargin"), g.get("projMargin"), g.get("spread")
    if spread is not None and sd and margin is not None:
        p_home = max(1e-6, min(1 - 1e-6, _ncdf((margin + spread) / sd)))
        if _spread_blocked(g, spread, p_home, g.get("sprHome")):
            out.append("Spread:home")
        if _spread_blocked(g, -spread, 1 - p_home, g.get("sprAway")):
            out.append("Spread:away")
    return out


def sharpen(games, props):
    """Adds each game's block list and drops prop sides that lose. Returns props."""
    for g in games:
        g["block"] = block_list(g)
    return [r for r in props
            if (r.get("sport"), r.get("marketLabel"), (r.get("side") or "").title()) not in BLOCKED_PROPS]
