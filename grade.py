#!/usr/bin/env python3
"""
grade.py — records every published pick, tracks closing-line value, grades results.

Runs right after edge_slate.py in the same workflow. It costs no odds credits:
it reads the slate that edge_slate.py just wrote, and gets final scores and
final player stat lines from ESPN, which is free.

What it does, in order:

  1. Reads edge_slate.json and derives the picks with the SAME rule the board
     uses — edge at or above the threshold for that group, EV > 0, edge no
     higher than the credible cap.
  2. Records any pick it has not seen before in picks_log.json, stamped with the
     price it was published at. That price never changes afterwards, so the
     record is of picks as published rather than as recomputed later.
  3. Appends the current price for every ungraded pick to that pick's history.
     The last price recorded before kickoff is treated as the closing line.
  4. Grades what has finished — game lines off the final score, player props off
     the box score — and computes closing-line value with the book's margin
     divided out.
  5. Writes results.json for the board, with SIDES AND PROPS KEPT APART.

Sides and props are separate records on purpose. They come from different
models fed by different data, they clear different thresholds, and mixing them
would hide a bad one behind a good one. A blended number would be the single
most misleading thing on the page.

Closing-line value matters more than early win rate. Two weeks of picks gives a
win rate with a confidence interval about nine points wide — it cannot tell a
real edge from a hot streak. Consistently beating the closing line is evidence
of an edge long before the results are statistically meaningful, because the
closing line is the sharpest estimate anyone has.

Env: EDGE_SLATE_PATH, PICKS_LOG_PATH, RESULTS_PATH, EDGE_THRESHOLD (default 5),
     PROPS_THRESHOLD (default 7), MAX_CREDIBLE_EDGE (default 20),
     MAX_PROPS_PER_GAME (default 2).
"""

import json
import math
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from picks_log import (PICKS_PATH, RESULTS_PATH, clv_points, edge_points,
                       expected_value, implied, load_picks, no_vig, pick_id,
                       save_picks, save_results, to_decimal)
from player_props import load_logs, refresh_logs, norm_name

SLATE_PATH = os.environ.get("EDGE_SLATE_PATH", "edge_slate.json")
THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "5"))
PROPS_THRESHOLD = float(os.environ.get("PROPS_THRESHOLD", "7"))
MAX_EDGE = float(os.environ.get("MAX_CREDIBLE_EDGE", "20"))

# Props on the same game share a game script: a quarterback going over his
# passing yards and his receiver going over his receiving yards are close to
# the same bet twice. Capping how many props ride on one game keeps a single
# blowout from taking out a whole day.
MAX_PROPS_PER_GAME = int(os.environ.get("MAX_PROPS_PER_GAME", "2"))

HTTP_TIMEOUT = 20

SIDE_MARKETS = ("Moneyline", "Spread")

# Every market the props model produces, named explicitly. Classifying by
# exclusion ("not a side, therefore a prop") silently swept the retired Total
# picks into the props record — three graded totals would have published a
# props record before a single prop had ever been graded.
PROP_MARKETS = (
    "Passing yards", "Rushing yards", "Receiving yards", "Receptions",
    "Anytime TD", "Points", "Rebounds", "Assists", "Pts+Reb+Ast",
)

# Totals are no longer published, and the ones already graded are removed from
# the ledger on sight. This is the one deletion the log allows, and it is a
# deliberate instruction rather than a rewrite of a live record: the market was
# dropped from the product, so its history goes with it.
DROPPED_MARKETS = ("Total",)

ESPN_PATHS = {
    "NFL": "football/nfl",
    "NCAAF": "football/college-football",
    "NBA": "basketball/nba",
    "NCAAB": "basketball/mens-college-basketball",
}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


def ncdf(z):
    """Standard normal CDF. Exact via erf, not an approximation."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def clamp01(x):
    return max(1e-6, min(1.0 - 1e-6, x))


def group_of(market, player=None):
    """Which record a pick belongs to. Positive tests only, never by exclusion."""
    if market in SIDE_MARKETS:
        return "Sides"
    if market in PROP_MARKETS or player:
        return "Props"
    return "Sides"


# --------------------------------------------------- derive today's picks ----
def candidates(g):
    """Both sides of the moneyline and the spread, with our probability for each.

    Mirrors the board exactly: one normal distribution over the game margin.
    Totals are no longer priced.
    """
    sd = g.get("sdMargin")
    if not sd or g.get("mlHome") is None or g.get("mlAway") is None:
        return []
    margin = g["homeRtg"] - g["awayRtg"] + g.get("hfa", 0.0)
    out = []

    p_home = clamp01(ncdf(margin / sd))
    out.append(dict(market="Moneyline", selection="home", line=None,
                    label=g["home"] + " ML", p=p_home,
                    price=g["mlHome"], other=g["mlAway"]))
    out.append(dict(market="Moneyline", selection="away", line=None,
                    label=g["away"] + " ML", p=1.0 - p_home,
                    price=g["mlAway"], other=g["mlHome"]))

    if g.get("spread") is not None and g.get("sprHome") is not None:
        spread = g["spread"]
        p_home_cover = clamp01(ncdf((margin + spread) / sd))
        out.append(dict(market="Spread", selection="home", line=spread,
                        label="%s %+.1f" % (g["home"], spread), p=p_home_cover,
                        price=g["sprHome"], other=g["sprAway"]))
        out.append(dict(market="Spread", selection="away", line=-spread,
                        label="%s %+.1f" % (g["away"], -spread),
                        p=1.0 - p_home_cover,
                        price=g["sprAway"], other=g["sprHome"]))
    return out


def side_picks(slate):
    """The best side of each game market, kept only when it clears the rule."""
    picks = []
    for g in slate.get("games", []):
        by_market = {}
        for c in candidates(g):
            if c["price"] is None:
                continue
            c["edge"] = edge_points(c["p"], c["price"])
            c["ev"] = expected_value(c["p"], c["price"])
            best = by_market.get(c["market"])
            if best is None or c["edge"] > best["edge"]:
                by_market[c["market"]] = c
        for c in by_market.values():
            if c["edge"] >= THRESHOLD and c["ev"] > 0 and c["edge"] <= MAX_EDGE:
                c.update(sport=g["sport"], home=g["home"], away=g["away"],
                         commence=g.get("time"), player=None, event=None,
                         proj=None, stat_line=None)
                picks.append(c)
    return picks


def prop_picks(slate):
    """Best side per player per market, capped per game, over the props bar."""
    best = {}
    for r in slate.get("props", []):
        if r.get("price") is None or r.get("p") is None:
            continue
        e = edge_points(r["p"], r["price"])
        v = expected_value(r["p"], r["price"])
        key = (r["eventId"], r["player"], r["market"])
        cur = best.get(key)
        if cur is None or e > cur["edge"]:
            best[key] = dict(r, edge=e, ev=v)

    keep = [c for c in best.values()
            if c["edge"] >= PROPS_THRESHOLD and c["ev"] > 0 and c["edge"] <= MAX_EDGE]

    # strongest first, then cap per game so one game script cannot carry the day
    keep.sort(key=lambda c: -c["edge"])
    per_game, picks = defaultdict(int), []
    for c in keep:
        if per_game[c["eventId"]] >= MAX_PROPS_PER_GAME:
            continue
        per_game[c["eventId"]] += 1
        picks.append(dict(
            market=c["marketLabel"], selection=c["side"].lower(), line=c.get("line"),
            label=c["label"], p=c["p"], price=c["price"], other=c.get("other"),
            edge=c["edge"], ev=c["ev"], sport=c["sport"], home=c["home"],
            away=c["away"], commence=c.get("time"), player=c["player"],
            event=c["eventId"], proj=c.get("proj"),
            stat_key=c["market"], stat_line=None))
    return picks


def todays_picks(slate):
    return side_picks(slate) + prop_picks(slate)


# ------------------------------------------------------------ final scores ----
def normalize(name):
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def finals_for(sport, days_back=12):
    """{(away_key, home_key): (away_score, home_score)} for completed games."""
    path = ESPN_PATHS.get(sport)
    out = {}
    if not path:
        return out
    today = date.today()
    for i in range(days_back):
        d = today - timedelta(days=i)
        try:
            r = requests.get(ESPN.format(path=path), timeout=HTTP_TIMEOUT,
                             params={"dates": d.strftime("%Y%m%d"), "limit": 400})
            if r.status_code != 200:
                continue
            events = r.json().get("events", [])
        except Exception:
            continue
        for ev in events:
            comp = (ev.get("competitions") or [{}])[0]
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            home = away = None
            for c in comp.get("competitors", []):
                nm = (c.get("team") or {}).get("displayName")
                try:
                    sc = float(c.get("score"))
                except (TypeError, ValueError):
                    continue
                if c.get("homeAway") == "home":
                    home = (nm, sc)
                else:
                    away = (nm, sc)
            if home and away:
                out[(normalize(away[0]), normalize(home[0]))] = (away[1], home[1])
        time.sleep(0.05)
    return out


def find_final(finals, away, home):
    key = (normalize(away), normalize(home))
    if key in finals:
        return finals[key]
    # fall back to a containment match — odds and ESPN spell a few schools
    # differently ("Miami" vs "Miami Hurricanes")
    a, h = normalize(away), normalize(home)
    for (fa, fh), score in finals.items():
        if (fa in a or a in fa) and (fh in h or h in fh):
            return score
    return None


def settle(pick, away_score, home_score):
    """('W'|'L'|'P', 'away-home') for a game-line pick against a final score."""
    label = "%d-%d" % (int(away_score), int(home_score))
    margin = home_score - away_score          # home perspective
    sel, line = pick["selection"], pick.get("line")

    if pick["market"] == "Moneyline":
        m = margin if sel == "home" else -margin
        return ("W" if m > 0 else "L" if m < 0 else "P"), label

    if pick["market"] == "Spread":
        own = margin if sel == "home" else -margin
        adj = own + line                      # line is already this side's number
        return ("W" if adj > 0 else "L" if adj < 0 else "P"), label

    return "P", label


# ------------------------------------------------------------- prop finals ----
# odds-api market key -> the stat name stored in player_logs.json
STAT_OF = {
    "player_pass_yds": "passYds", "player_rush_yds": "rushYds",
    "player_reception_yds": "recYds", "player_receptions": "receptions",
    "player_anytime_td": "tds", "player_points": "points",
    "player_rebounds": "rebounds", "player_assists": "assists",
    "player_points_rebounds_assists": "pra",
}


def player_finals(sports, diag):
    """{(sport, event_id, normalized player): {stat: value}} for finished games.

    Reads the same box-score cache the projections are built from, refreshing it
    first so a standalone grading run still sees last night's games.
    """
    out = {}
    for s in sports:
        if s not in ("NFL", "NBA"):
            continue
        try:
            refresh_logs(s, diag)
        except Exception as e:
            diag.append("could not refresh %s box scores for grading — %s" % (s, e))
    logs = load_logs()
    for s in sports:
        for g in logs.get(s, {}).get("games", []):
            out[(s, str(g.get("event")), norm_name(g.get("player")))] = g
    return out


def settle_prop(pick, stats):
    """('W'|'L'|'V', 'stat line') for a prop, or None if the box score is absent.

    A player with no line in the box score did not take the field, so the book
    voids the bet — 'V', excluded from the record entirely rather than counted
    as a loss. Voids are never wins for us either; this stays deliberately
    conservative.
    """
    stat = STAT_OF.get(pick.get("stat_key") or "")
    if not stat:
        return None
    if stats is None:
        return "V", "did not play"
    actual = float(stats.get(stat, 0.0))

    if stat == "tds":
        won = actual >= 1
        return ("W" if won else "L"), ("%d TD" % int(actual))

    line = pick.get("line")
    if line is None:
        return None
    # every stored prop line is a half-point, so there is no push to handle
    over_won = actual > line
    won = over_won if pick["selection"] == "over" else not over_won
    return ("W" if won else "L"), ("%g" % actual)


# ------------------------------------------------------------------- main ----
def week_of(iso):
    """A stable Monday-anchored week label, so a whole card grades as one week."""
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return "Unscheduled"
    monday = d.date() - timedelta(days=d.weekday())
    return "Week of %s %d" % (monday.strftime("%b"), monday.day)


def summarize(picks):
    """Record, win rate and CLV for one group of graded picks."""
    W = sum(1 for p in picks if p["result"] == "W")
    L = sum(1 for p in picks if p["result"] == "L")
    P = sum(1 for p in picks if p["result"] == "P")
    V = sum(1 for p in picks if p["result"] == "V")
    clv = [p["clv"] for p in picks if p.get("clv") is not None]
    return {
        "record": {"w": W, "l": L, "p": P, "void": V},
        "win_rate": (W / (W + L)) if (W + L) else None,
        "avg_clv": (sum(clv) / len(clv)) if clv else None,
        "clv_beat_rate": (sum(1 for c in clv if c > 0) / len(clv)) if clv else None,
        "clv_n": len(clv),
        "graded": W + L + P,
    }


def main():
    diag = []
    try:
        with open(SLATE_PATH) as f:
            slate = json.load(f)
    except (OSError, ValueError) as e:
        print("no slate to read (%s) — nothing to record" % e)
        slate = {"games": [], "props": []}

    log = load_picks()
    dropped = [p for p in log["picks"] if p.get("market") in DROPPED_MARKETS]
    if dropped:
        log["picks"] = [p for p in log["picks"] if p.get("market") not in DROPPED_MARKETS]
        print("removed %d retired total pick(s) from the ledger" % len(dropped))
    by_id = {p["id"]: p for p in log["picks"]}
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    added = updated = 0

    for c in todays_picks(slate):
        pid = pick_id(c["sport"], c["away"], c["home"], c["market"], c["label"])
        rec = by_id.get(pid)
        if rec is None:
            rec = {
                "id": pid, "sport": c["sport"], "away": c["away"], "home": c["home"],
                "commence": c["commence"], "market": c["market"],
                "group": group_of(c["market"], c.get("player")),
                "selection": c["selection"], "line": c["line"], "pick": c["label"],
                "player": c.get("player"), "event": c.get("event"),
                "stat_key": c.get("stat_key"), "proj": c.get("proj"),
                "p": round(c["p"], 6),
                "price": c["price"], "other_price": c.get("other"),
                "edge": round(c["edge"], 3), "ev": round(c["ev"], 5),
                "first_seen": now_iso,
                "history": [], "result": None, "final": None, "clv": None,
                "week": week_of(c["commence"]),
            }
            log["picks"].append(rec)
            by_id[pid] = rec
            added += 1
        rec.setdefault("group", group_of(rec.get("market", ""), rec.get("player")))
        # a record written before the groups were split may carry the wrong one
        rec["group"] = group_of(rec.get("market", ""), rec.get("player"))
        # line history: only until the game starts, and only while ungraded
        if rec.get("result") is None:
            started = False
            try:
                started = datetime.fromisoformat(
                    (rec.get("commence") or "").replace("Z", "+00:00")) <= now
            except ValueError:
                pass
            if not started:
                rec["history"].append([now_iso, c["price"], c.get("other")])
                updated += 1

    # ------------------------------------------------------------- grading --
    pending = [p for p in log["picks"] if p.get("result") is None]
    sports = {p["sport"] for p in pending}
    finals = {}
    for s in sports:
        finals[s] = finals_for(s)
        print("%s: %d finished games found" % (s, len(finals[s])))

    prop_sports = {p["sport"] for p in pending
                   if group_of(p.get("market", ""), p.get("player")) == "Props"}
    box = player_finals(prop_sports, diag) if prop_sports else {}

    graded_now = 0
    for p in log["picks"]:
        if p.get("result") is not None:
            continue
        grp = group_of(p.get("market", ""), p.get("player"))

        if grp == "Sides":
            score = find_final(finals.get(p["sport"], {}), p["away"], p["home"])
            if not score:
                continue
            away_score, home_score = score
            p["result"], p["final"] = settle(p, away_score, home_score)
        else:
            # only grade a prop once the game itself is final, never mid-game
            score = find_final(finals.get(p["sport"], {}), p["away"], p["home"])
            if not score:
                continue
            stats = box.get((p["sport"], str(p.get("event")), norm_name(p.get("player"))))
            outcome = settle_prop(p, stats)
            if outcome is None:
                continue
            p["result"], p["final"] = outcome

        if p["history"] and p.get("other_price") is not None:
            close = p["history"][-1]
            if close[2] is not None:
                p["clv"] = round(clv_points(p["price"], p["other_price"],
                                            close[1], close[2]), 3)
                p["closing_price"] = close[1]
        graded_now += 1

    save_picks(log)

    # -------------------------------------------------------------- output --
    graded = [p for p in log["picks"] if p.get("result")]
    for p in graded:
        p["group"] = group_of(p.get("market", ""), p.get("player"))
    sides = [p for p in graded if p["group"] == "Sides"]
    props = [p for p in graded if p["group"] == "Props"]

    def rows(items):
        return [
            {
                "week": p["week"], "sport": p["sport"], "group": p["group"],
                "game": p["away"] + " @ " + p["home"],
                "market": p["market"], "pick": p["pick"],
                "player": p.get("player"), "proj": p.get("proj"),
                "price": p["price"], "closing_price": p.get("closing_price"),
                "edge": p["edge"], "ev": p["ev"],
                "result": p["result"], "final": p["final"], "clv": p.get("clv"),
            }
            for p in items
        ]

    by_sport = {}
    for p in graded:
        key = "%s %s" % (p["sport"], p["group"])
        by_sport.setdefault(key, []).append(p)

    groups = {
        "Sides": dict(summarize(sides), label="Moneyline & spread",
                      threshold=THRESHOLD, picks=rows(sides)),
        "Props": dict(summarize(props), label="Player props",
                      threshold=PROPS_THRESHOLD, picks=rows(props)),
    }

    save_results({
        "generated_at": now_iso,
        "graded_through": max((p["week"] for p in graded), default=None),
        "groups": groups,
        "by_sport": {k: summarize(v) for k, v in sorted(by_sport.items())},
        "pending": {
            "Sides": sum(1 for p in log["picks"] if p.get("result") is None
                         and group_of(p.get("market", ""), p.get("player")) == "Sides"),
            "Props": sum(1 for p in log["picks"] if p.get("result") is None
                         and group_of(p.get("market", ""), p.get("player")) == "Props"),
        },
        "diagnostics": diag,
        "note": "Sides and props are tracked separately: different models, "
                "different data, different thresholds, so a blended number "
                "would hide one behind the other. Picks are recorded when "
                "published, at the price published. Closing-line value compares "
                "that price with the last price seen before kickoff, with the "
                "book's margin divided out. Win rate counts wins and losses "
                "only. A prop on a player who never took the field is voided by "
                "the book and is excluded from the record rather than counted.",
    })

    print("recorded %d new picks, %d line updates, graded %d" % (added, updated, graded_now))
    for name, items in (("sides", sides), ("props", props)):
        s = summarize(items)
        r = s["record"]
        line = "%s: %d-%d" % (name, r["w"], r["l"])
        if r["p"]:
            line += " (%d push)" % r["p"]
        if r["void"]:
            line += " (%d void)" % r["void"]
        if s["avg_clv"] is not None:
            line += ", average CLV %+.2f points over %d" % (s["avg_clv"], s["clv_n"])
        print(line)
    for d in diag:
        print("  " + d)


if __name__ == "__main__":
    main()
