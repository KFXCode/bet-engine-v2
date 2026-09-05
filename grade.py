#!/usr/bin/env python3
"""
grade.py — records every published pick, tracks closing-line value, grades results.

Runs after edge_slate.py in the same workflow. Free: it reads the slate already
on disk and gets final scores from ESPN. No odds-API credits are spent here.

ONE PICK PER GAME + MARKET + SIDE.
A pick's identity deliberately excludes the line and the price. "Over" on a
given game is the same bet whether the number is 50.5 or 52.5 — if the identity
included the line, every pre-kickoff refresh would mint a NEW pick and the same
opinion would grade three or four times off one game. That inflates a losing
opinion into several losses and makes the record meaningless.

So: the FIRST publication owns the record. Its line, price, probability and edge
are frozen at that moment and are what the pick grades against. Later refreshes
that re-flag the same side at a different number are recorded in `line_moves`
and surfaced as a short note — they never create a second pick and never affect
the record.

Env: EDGE_SLATE_PATH (default edge_slate.json), PICKS_LOG_PATH, RESULTS_PATH,
     EDGE_THRESHOLD (default 5), MAX_CREDIBLE_EDGE (default 20).
"""

import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

from picks_log import (PICKS_PATH, RESULTS_PATH, clv_points, edge_points,
                       expected_value, implied, load_picks, no_vig,
                       save_picks, save_results, to_decimal)

SLATE_PATH = os.environ.get("EDGE_SLATE_PATH", "edge_slate.json")
THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "5"))
MAX_EDGE = float(os.environ.get("MAX_CREDIBLE_EDGE", "20"))
HTTP_TIMEOUT = 20

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


def identity(sport, away, home, market, selection):
    """What makes two entries THE SAME BET.

    Note what is absent: the line and the price. Those move; the opinion does
    not. Including them would let one opinion grade many times.
    """
    return "|".join([sport, away + " @ " + home, market, selection])


# --------------------------------------------------- derive today's picks ----
def candidates(g):
    """Every side of every market in one game, with our probability for it."""
    sd, sd_t = g.get("sdMargin"), g.get("sdTotal")
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

    if g.get("total") is not None and g.get("over") is not None and sd_t:
        total = g["total"]
        p_over = clamp01(ncdf((g["projTotal"] - total) / sd_t))
        out.append(dict(market="Total", selection="over", line=total,
                        label="Over %s" % total, p=p_over,
                        price=g["over"], other=g["under"]))
        out.append(dict(market="Total", selection="under", line=total,
                        label="Under %s" % total, p=1.0 - p_over,
                        price=g["under"], other=g["over"]))
    return out


def todays_picks(slate):
    """The best side of each market, kept only when it clears the rule."""
    picks = []
    for g in slate.get("games", []):
        by_market = {}
        for c in candidates(g):
            c["edge"] = edge_points(c["p"], c["price"])
            c["ev"] = expected_value(c["p"], c["price"])
            best = by_market.get(c["market"])
            if best is None or c["edge"] > best["edge"]:
                by_market[c["market"]] = c
        for c in by_market.values():
            if c["edge"] >= THRESHOLD and c["ev"] > 0 and c["edge"] <= MAX_EDGE:
                c.update(sport=g["sport"], home=g["home"], away=g["away"],
                         commence=g.get("time"))
                picks.append(c)
    return picks


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
            # Only a FINAL game may be graded. Grading mid-game marks outcomes
            # that have not happened yet as losses.
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
    a, h = normalize(away), normalize(home)
    for (fa, fh), score in finals.items():
        if (fa in a or a in fa) and (fh in h or h in fh):
            return score
    return None


def settle(pick, away_score, home_score):
    """('W'|'L'|'P', 'away-home') against the pick's ORIGINAL line."""
    label = "%d-%d" % (int(away_score), int(home_score))
    margin = home_score - away_score          # home perspective
    total = home_score + away_score
    sel, line = pick["selection"], pick.get("line")

    if pick["market"] == "Moneyline":
        m = margin if sel == "home" else -margin
        return ("W" if m > 0 else "L" if m < 0 else "P"), label

    if pick["market"] == "Spread":
        own = margin if sel == "home" else -margin
        adj = own + line                      # line is already this side's number
        return ("W" if adj > 0 else "L" if adj < 0 else "P"), label

    if pick["market"] == "Total":
        diff = total - line
        if diff == 0:
            return "P", label
        over_won = diff > 0
        won = over_won if sel == "over" else not over_won
        return ("W" if won else "L"), label

    return "P", label


def week_of(iso):
    """A stable Monday-anchored week label, so a whole card grades as one week."""
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return "Unscheduled"
    monday = d.date() - timedelta(days=d.weekday())
    return "Week of %s %d" % (monday.strftime("%b"), monday.day)


def line_text(pick):
    """A short human note about how the number moved after we published."""
    moves = pick.get("line_moves") or []
    if not moves:
        return None
    seen, ordered = set(), []
    for m in moves:
        key = (m.get("line"), m.get("price"))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(m)
    if not ordered:
        return None
    last = ordered[-1]
    if pick.get("line") is not None and last.get("line") is not None:
        if abs(float(last["line"]) - float(pick["line"])) > 1e-9:
            return "line moved %+.1f \u2192 %+.1f after we posted (graded at %+.1f)" % (
                float(pick["line"]), float(last["line"]), float(pick["line"]))
    if last.get("price") is not None and last["price"] != pick.get("price"):
        return "price moved %s \u2192 %s after we posted (graded at %s)" % (
            pick["price"], last["price"], pick["price"])
    return None


# ------------------------------------------------------ migrate old ledger ----
def collapse(log):
    """Fold pre-existing duplicates into one pick each.

    Earlier versions keyed a pick by its label, which carried the line, so a
    moved line minted a second pick off the same opinion. Anything already in
    the ledger is merged here: the earliest publication survives with its
    original line and price, and the rest become line_moves.
    """
    merged, order, dropped = {}, [], 0
    for p in log.get("picks", []):
        key = identity(p["sport"], p["away"], p["home"], p["market"],
                       p["selection"])
        first = merged.get(key)
        if first is None:
            p["id"] = key
            p.setdefault("line_moves", [])
            merged[key] = p
            order.append(key)
            continue
        dropped += 1
        # keep whichever was published first; the other becomes a line move
        older, newer = first, p
        if (p.get("first_seen") or "") < (first.get("first_seen") or ""):
            older, newer = p, first
            older["id"] = key
            older.setdefault("line_moves", first.get("line_moves", []))
            merged[key] = older
        older.setdefault("line_moves", []).append({
            "at": newer.get("first_seen"),
            "line": newer.get("line"),
            "price": newer.get("price"),
        })
        # the surviving pick keeps the longest price history it has seen
        if len(newer.get("history") or []) > len(older.get("history") or []):
            older["history"] = newer["history"]
        # a duplicate that was already graded contributes nothing; the survivor
        # regrades from its own original line below
        older["result"] = None
        older["final"] = None
        older["clv"] = None
        older.pop("closing_price", None)
    if dropped:
        print("Collapsed %d duplicate entries created by line movement. "
              "Each opinion is now one pick, graded at the line it was "
              "published at." % dropped)
    log["picks"] = [merged[k] for k in order]
    return log


def main():
    try:
        with open(SLATE_PATH) as f:
            slate = json.load(f)
    except (OSError, ValueError) as e:
        print("no slate to read (%s) — nothing to record" % e)
        return

    log = collapse(load_picks())
    by_id = {p["id"]: p for p in log["picks"]}
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    added = updated = moves = 0

    for c in todays_picks(slate):
        pid = identity(c["sport"], c["away"], c["home"], c["market"],
                       c["selection"])
        rec = by_id.get(pid)

        if rec is None:
            rec = {
                "id": pid, "sport": c["sport"], "away": c["away"], "home": c["home"],
                "commence": c["commence"], "market": c["market"],
                "selection": c["selection"],
                # frozen at first publication — never rewritten
                "line": c["line"], "pick": c["label"],
                "p": round(c["p"], 6),
                "price": c["price"], "other_price": c["other"],
                "edge": round(c["edge"], 3), "ev": round(c["ev"], 5),
                "first_seen": now_iso,
                "history": [], "line_moves": [],
                "result": None, "final": None, "clv": None,
                "week": week_of(c["commence"]),
            }
            log["picks"].append(rec)
            by_id[pid] = rec
            added += 1
        elif rec.get("result") is None:
            # Same opinion, new number. Record the movement; change nothing that
            # the record depends on.
            same_line = (rec.get("line") is None and c["line"] is None) or (
                rec.get("line") is not None and c["line"] is not None
                and abs(float(rec["line"]) - float(c["line"])) < 1e-9)
            if not same_line or c["price"] != rec.get("price"):
                rec.setdefault("line_moves", []).append(
                    {"at": now_iso, "line": c["line"], "price": c["price"]})
                moves += 1

        # line history for CLV: only before kickoff, only while ungraded
        if rec.get("result") is None:
            started = False
            try:
                started = datetime.fromisoformat(
                    (rec.get("commence") or "").replace("Z", "+00:00")) <= now
            except ValueError:
                pass
            if not started:
                rec["history"].append([now_iso, c["price"], c["other"]])
                updated += 1

    # grade anything that has finished, against its ORIGINAL line
    sports = {p["sport"] for p in log["picks"] if p.get("result") is None}
    finals = {}
    for s in sports:
        finals[s] = finals_for(s)
        print("%s: %d finished games found" % (s, len(finals[s])))

    graded_now = 0
    for p in log["picks"]:
        if p.get("result") is not None:
            continue
        score = find_final(finals.get(p["sport"], {}), p["away"], p["home"])
        if not score:
            continue
        away_score, home_score = score
        p["result"], p["final"] = settle(p, away_score, home_score)
        if p["history"]:
            close = p["history"][-1]
            p["clv"] = round(clv_points(p["price"], p["other_price"],
                                        close[1], close[2]), 3)
            p["closing_price"] = close[1]
        graded_now += 1

    save_picks(log)

    graded = [p for p in log["picks"] if p.get("result")]
    W = sum(1 for p in graded if p["result"] == "W")
    L = sum(1 for p in graded if p["result"] == "L")
    P = sum(1 for p in graded if p["result"] == "P")
    with_clv = [p["clv"] for p in graded if p.get("clv") is not None]

    save_results({
        "generated_at": now_iso,
        "graded_through": max((p["week"] for p in graded), default=None),
        "record": {"w": W, "l": L, "p": P},
        "win_rate": (W / (W + L)) if (W + L) else None,
        "avg_clv": (sum(with_clv) / len(with_clv)) if with_clv else None,
        "clv_beat_rate": (sum(1 for c in with_clv if c > 0) / len(with_clv)) if with_clv else None,
        "pending": sum(1 for p in log["picks"] if p.get("result") is None),
        "note": "One pick per game, market and side. It is recorded at the "
                "price and line it was first published at, and graded against "
                "that number. Later line movement is noted but never regraded "
                "and never counted again. Win rate counts wins and losses only.",
        "picks": [
            {
                "week": p["week"], "sport": p["sport"],
                "game": p["away"] + " @ " + p["home"],
                "market": p["market"], "pick": p["pick"],
                "price": p["price"], "closing_price": p.get("closing_price"),
                "edge": p["edge"], "ev": p["ev"],
                "result": p["result"], "final": p["final"], "clv": p.get("clv"),
                "line_note": line_text(p),
            }
            for p in graded
        ],
    })

    print("recorded %d new picks, %d line updates, %d line moves noted, graded %d"
          % (added, updated, moves, graded_now))
    print("record %d-%d (%d push), %d pending"
          % (W, L, P, sum(1 for p in log["picks"] if p.get("result") is None)))
    if with_clv:
        print("average CLV %+.2f points over %d graded picks"
              % (sum(with_clv) / len(with_clv), len(with_clv)))


if __name__ == "__main__":
    main()
