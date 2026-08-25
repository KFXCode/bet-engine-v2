#!/usr/bin/env python3
"""
edge_slate.py — publishes edge_slate.json for the Edge Engine board.

It needs nothing from you: it computes its OWN team ratings from finished
games, pulls today's FanDuel lines, and writes edge_slate.json. The board
fetches that file and does the EV math.

  ratings      iterative adjusted margin (SRS-style), fit JOINTLY with home
               field: given ratings, hfa is the part of the home margin the
               ratings do not explain; given hfa, ratings are re-fit; repeat 40
               times, re-centering to mean 0. Strength of schedule falls out of
               it, and hfa is not inflated by strong teams hosting weak ones.
  hfa          points of home advantage, from that joint fit.
  sdMargin     RMSE of (rating gap + hfa) against actual margins.
  projTotal    own points/game + opponent points allowed/game - league average,
               both sides summed. sdTotal is that projection's RMSE.

Markets on a whole number (spread -3, total 44) are DROPPED: a push is a third
outcome this model does not price. Half-point lines only.

Env: ODDS_API_KEY
"""

import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
OUT_PATH = os.environ.get("EDGE_SLATE_PATH", "edge_slate.json")
HTTP_TIMEOUT = 20

# league -> (odds-api key, espn path, days of results to fit on)
# 400 days so a league between seasons still fits on last season and can price
# its opening week. Anything shorter goes blank in the offseason.
LEAGUES = {
    "NFL":   ("americanfootball_nfl",   "football/nfl",         400),
    "NCAAF": ("americanfootball_ncaaf", "football/college-football", 400),
    "NBA":   ("basketball_nba",         "basketball/nba",       400),
    "NCAAB": ("basketball_ncaab",       "basketball/mens-college-basketball", 400),
}

ESPN = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
ODDS = "https://api.the-odds-api.com/v4/sports/{key}/odds"


# ---------------------------------------------------------------- results ----
def finished_games(espn_path, days):
    """[(home, away, home_score, away_score)] for completed games."""
    out, today = [], date.today()
    for i in range(days):
        d = today - timedelta(days=i + 1)
        try:
            r = requests.get(ESPN.format(path=espn_path),
                             params={"dates": d.strftime("%Y%m%d"), "limit": 400},
                             timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            events = r.json().get("events", [])
        except Exception:
            continue
        for ev in events:
            comp = (ev.get("competitions") or [{}])[0]
            if not (comp.get("status", {}).get("type", {}).get("completed")):
                continue
            home = away = None
            for c in comp.get("competitors", []):
                name = (c.get("team") or {}).get("displayName")
                try:
                    score = float(c.get("score"))
                except (TypeError, ValueError):
                    continue
                if c.get("homeAway") == "home":
                    home = (name, score)
                else:
                    away = (name, score)
            if home and away:
                out.append((home[0], away[0], home[1], away[1]))
        time.sleep(0.05)
    return out


def fit(games):
    """Ratings, home-field edge, and the two standard deviations.

    Home-field and ratings are fit JOINTLY. Estimating hfa as the plain average
    home margin is wrong wherever strong teams host weak ones (all of college
    football and basketball) — it credits the venue for the mismatch and
    inflates hfa to 8-9 points. Instead: given ratings, hfa is the average of
    what the ratings FAIL to explain; given hfa, ratings are re-fit. Repeat.
    """
    if len(games) < 30:
        return None

    hfa = sum(h - a for _, _, h, a in games) / len(games)   # starting guess

    opponents = defaultdict(list)       # team -> [(opponent, own raw margin, is_home)]
    pts_for, pts_against, count = defaultdict(float), defaultdict(float), defaultdict(int)
    for home, away, hs, as_ in games:
        opponents[home].append((away, hs - as_, True))
        opponents[away].append((home, as_ - hs, False))
        pts_for[home] += hs; pts_against[home] += as_; count[home] += 1
        pts_for[away] += as_; pts_against[away] += hs; count[away] += 1

    rating = {t: 0.0 for t in opponents}
    for _ in range(40):
        nxt = {}
        for t, gs in opponents.items():
            # own margin, with the venue effect and the opponent's strength removed
            nxt[t] = sum(m - (hfa if home else -hfa) + rating.get(opp, 0.0)
                         for opp, m, home in gs) / len(gs)
        mean = sum(nxt.values()) / len(nxt)
        rating = {t: v - mean for t, v in nxt.items()}
        # whatever the ratings cannot explain about playing at home IS home field
        hfa = sum((hs - as_) - (rating.get(h, 0.0) - rating.get(a, 0.0))
                  for h, a, hs, as_ in games) / len(games)

    lg_ppg = sum(pts_for.values()) / sum(count.values())
    off = {t: pts_for[t] / count[t] for t in count}
    dfn = {t: pts_against[t] / count[t] for t in count}

    def proj_total(home, away):
        eh = off.get(home, lg_ppg) + dfn.get(away, lg_ppg) - lg_ppg
        ea = off.get(away, lg_ppg) + dfn.get(home, lg_ppg) - lg_ppg
        return eh + ea

    n = len(games)
    sd_margin = (sum(((hs - as_) - (rating.get(h, 0) - rating.get(a, 0) + hfa)) ** 2
                     for h, a, hs, as_ in games) / n) ** 0.5
    sd_total = (sum(((hs + as_) - proj_total(h, a)) ** 2
                    for h, a, hs, as_ in games) / n) ** 0.5
    return {"rating": rating, "hfa": hfa, "sd_margin": sd_margin,
            "sd_total": sd_total, "proj_total": proj_total, "games": n}


# ------------------------------------------------------------------- odds ----
def fanduel_lines(sport_key, diag):
    if not ODDS_API_KEY:
        diag.append("ODDS_API_KEY is empty — no odds were requested at all")
        return []
    r = requests.get(ODDS.format(key=sport_key), timeout=HTTP_TIMEOUT, params={
        "apiKey": ODDS_API_KEY, "regions": "us", "oddsFormat": "american",
        "markets": "h2h,spreads,totals", "bookmakers": "fanduel"})
    if r.status_code != 200:
        msg = f"odds api {sport_key}: HTTP {r.status_code} {r.text[:160]}"
        print("  " + msg)
        diag.append(msg)
        return []
    events = r.json()
    diag.append(f"{sport_key}: odds api returned {len(events)} events")
    return events


def half_point(x):
    return x is not None and abs(x * 2) % 2 == 1


def market(book, key):
    for m in book.get("markets", []):
        if m.get("key") == key:
            return {o.get("name"): o for o in m.get("outcomes", [])}
    return {}


# ------------------------------------------------------------------ build ----
def build(diag):
    out = []
    for league, (sport_key, espn_path, days) in LEAGUES.items():
        print(f"{league}: fitting ratings…")
        results = finished_games(espn_path, days)
        model = fit(results)
        if not model:
            msg = (f"{league}: only {len(results)} finished games found in "
                   f"{days} days — need 30, league skipped")
            print("  " + msg)
            diag.append(msg)
            continue
        diag.append(f"{league}: fit on {model['games']} games, hfa "
                    f"{model['hfa']:.2f}, sd margin {model['sd_margin']:.2f}")
        print(f"  {model['games']} games · hfa {model['hfa']:.2f} · "
              f"sd margin {model['sd_margin']:.2f} · sd total {model['sd_total']:.2f}")

        no_rating = no_h2h = 0
        for ev in fanduel_lines(sport_key, diag):
            home, away = ev.get("home_team"), ev.get("away_team")
            books = ev.get("bookmakers") or []
            if not (home and away and books):
                continue
            fd = books[0]
            h2h, spreads, totals = market(fd, "h2h"), market(fd, "spreads"), market(fd, "totals")
            if home not in h2h or away not in h2h:
                no_h2h += 1
                continue
            if home not in model["rating"] or away not in model["rating"]:
                no_rating += 1
                print(f"  no rating for {away} @ {home} — skipped")
                continue

            g = {
                "sport": league, "home": home, "away": away,
                "time": ev.get("commence_time"),
                "homeRtg": round(model["rating"].get(home, 0.0), 3),
                "awayRtg": round(model["rating"].get(away, 0.0), 3),
                "hfa": round(model["hfa"], 3),
                "sdMargin": round(model["sd_margin"], 3),
                "sdTotal": round(model["sd_total"], 3),
                "projTotal": round(model["proj_total"](home, away), 2),
                "mlHome": h2h[home].get("price"), "mlAway": h2h[away].get("price"),
            }

            hs = spreads.get(home, {}).get("point")
            if home in spreads and away in spreads and half_point(hs):
                g["spread"] = hs
                g["sprHome"] = spreads[home].get("price")
                g["sprAway"] = spreads[away].get("price")

            tl = totals.get("Over", {}).get("point")
            if "Over" in totals and "Under" in totals and half_point(tl):
                g["total"] = tl
                g["over"] = totals["Over"].get("price")
                g["under"] = totals["Under"].get("price")

            out.append(g)
        priced = sum(1 for x in out if x["sport"] == league)
        diag.append(f"{league}: {priced} priced, {no_rating} dropped for a "
                    f"missing rating, {no_h2h} with no FanDuel moneyline")
        print(f"  {priced} games priced")
    return out


def main():
    diag = []
    games = build(diag)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "adjusted-margin ratings fit on finished games; normal margin "
                 "and total distributions; half-point lines only",
        "diagnostics": diag,
        "games": games,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"wrote {OUT_PATH} — {len(games)} games")


if __name__ == "__main__":
    main()
