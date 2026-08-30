#!/usr/bin/env python3
"""
edge_slate.py — publishes edge_slate.json for the Edge Engine board.

It needs nothing from you: it computes its OWN team ratings from finished
games, pulls FanDuel lines for the games starting soon, and writes
edge_slate.json. The board fetches that file and does the EV math.

What it computes (all of it from data, none of it hand-entered):

  ratings      iterative adjusted margin (SRS-style), fit JOINTLY with home
               field: given ratings, hfa is the part of the home margin the
               ratings do not explain; given hfa, ratings are re-fit; repeat 40
               times, re-centering to mean 0. Strength of schedule falls out of
               it, and hfa is not inflated by strong teams hosting weak ones.
               Ratings are then regressed toward average unless the league has
               actually played recently.
  hfa          points of home advantage, from that joint fit.
  sdMargin     root-mean-square error of (rating_home - rating_away + hfa)
               against actual margins — the real spread of game results.
  projTotal    expected points for each side = own points/game
               + opponent points allowed/game - league average points/game,
               summed. sdTotal is the RMSE of that against actual totals.

Markets on a whole number (spread -3, total 44) are DROPPED: a push is a third
outcome and this model does not price it. Half-point lines only.

Env: ODDS_API_KEY, and optionally ODDS_API_KEY_BACKUP. Keys are tried in order
and a key that reports out-of-credits is dropped for the rest of the run, so a
spent key falls through to the next one without losing a market.
"""

import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

ODDS_API_KEYS = [k.strip() for k in (
    os.environ.get("ODDS_API_KEY", "") + "," +
    os.environ.get("ODDS_API_KEY_BACKUP", "")
).split(",") if k.strip()]
OUT_PATH = os.environ.get("EDGE_SLATE_PATH", "edge_slate.json")
HTTP_TIMEOUT = 20
EXHAUSTED = set()      # keys that reported out-of-credits during this run

# Only price games starting inside this many days. Books post Christmas and
# October lines in August; those are not today's card.
DAYS_AHEAD = int(os.environ.get("EDGE_DAYS_AHEAD", "7"))

# Games between DAYS_AHEAD and LOOKAHEAD_DAYS out are published WITHOUT a
# probability, for the lookahead view only. They are not priced on purpose: the
# ratings that would price them are weeks of games away from being right.
LOOKAHEAD_DAYS = int(os.environ.get("EDGE_LOOKAHEAD_DAYS", "45"))

# Preseason regression. Last season's rating is not this season's team — rosters
# turn over, and an un-regressed rating invents edges that are not there. Shrink
# every rating toward 0 (average), easing the shrink off as recent games
# accumulate; a league with GAMES_TO_TRUST games per team inside the window is
# trusted in full.
GAMES_TO_TRUST = {"NFL": 8, "NCAAF": 6, "NBA": 20, "NCAAB": 15}
MAX_SHRINK = 0.35      # with no recent games, keep 65% of the fitted rating
RECENT_WINDOW = 60     # days that count as "recently played"

# league -> (odds-api key, espn path, days of results to fit on)
# 400 days so a league that is between seasons still fits on last season and can
# price its opening week. Anything shorter goes blank in the offseason.
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
                out.append((home[0], away[0], home[1], away[1], d))
        time.sleep(0.05)
    return out


def recent_form(results):
    """Games per team played in the last RECENT_WINDOW days.

    Deliberately a fixed window rather than 'this season'. Detecting a season
    boundary by looking for a gap in the schedule kept getting fooled — spring
    games, exhibitions and last season's tail all read as a season in progress,
    which left August ratings at full strength when no 2026 game had been
    played. A plain 60-day count cannot be fooled: in the offseason it is zero,
    and by midseason it is high enough to trust the ratings in full.
    """
    if not results:
        return 0.0
    since = date.today() - timedelta(days=RECENT_WINDOW)
    recent = [g for g in results if g[4] >= since]
    if not recent:
        return 0.0
    teams = {t for g in recent for t in (g[0], g[1])}
    return 2.0 * len(recent) / len(teams)


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

    hfa = sum(h - a for _, _, h, a, _ in games) / len(games)   # starting guess

    opponents = defaultdict(list)       # team -> [(opponent, own raw margin, is_home)]
    pts_for, pts_against, count = defaultdict(float), defaultdict(float), defaultdict(int)
    for home, away, hs, as_, _ in games:
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
                  for h, a, hs, as_, _ in games) / len(games)

    lg_ppg = sum(pts_for.values()) / sum(count.values())
    off = {t: pts_for[t] / count[t] for t in count}
    dfn = {t: pts_against[t] / count[t] for t in count}

    def proj_total(home, away):
        eh = off.get(home, lg_ppg) + dfn.get(away, lg_ppg) - lg_ppg
        ea = off.get(away, lg_ppg) + dfn.get(home, lg_ppg) - lg_ppg
        return eh + ea

    n = len(games)
    sd_margin = (sum(((hs - as_) - (rating.get(h, 0) - rating.get(a, 0) + hfa)) ** 2
                     for h, a, hs, as_, _ in games) / n) ** 0.5
    sd_total = (sum(((hs + as_) - proj_total(h, a)) ** 2
                    for h, a, hs, as_, _ in games) / n) ** 0.5
    return {"rating": rating, "hfa": hfa, "sd_margin": sd_margin,
            "sd_total": sd_total, "proj_total": proj_total, "games": n}


# ------------------------------------------------------------------- odds ----
def fanduel_lines(sport_key, diag):
    """Try each key in turn; a key that is out of credits is skipped from then on."""
    if not ODDS_API_KEYS:
        diag.append("no odds api key set — no lines were requested")
        return []
    for i, key in enumerate(ODDS_API_KEYS):
        if key in EXHAUSTED:
            continue
        label = "primary" if i == 0 else f"backup {i}"
        try:
            r = requests.get(ODDS.format(key=sport_key), timeout=HTTP_TIMEOUT, params={
                "apiKey": key, "regions": "us", "oddsFormat": "american",
                "markets": "h2h,spreads,totals", "bookmakers": "fanduel"})
        except Exception as e:
            diag.append(f"{sport_key}: {label} key request failed — {e}")
            continue
        if r.status_code == 200:
            events = r.json()
            left = r.headers.get("x-requests-remaining")
            diag.append(f"{sport_key}: {len(events)} events from the {label} key"
                        + (f", {left} credits left on it" if left else ""))
            return events
        body = r.text[:160]
        if r.status_code in (401, 429) and ("USAGE" in body.upper() or "QUOTA" in body.upper()):
            EXHAUSTED.add(key)
            diag.append(f"{label} key is out of credits — falling back to the next key")
            continue
        diag.append(f"odds api {sport_key} on the {label} key: HTTP {r.status_code} {body}")
        return []
    diag.append(f"{sport_key}: every key is out of credits")
    return []


def half_point(x):
    return x is not None and abs(x * 2) % 2 == 1


def market(book, key):
    for m in book.get("markets", []):
        if m.get("key") == key:
            return {o.get("name"): o for o in m.get("outcomes", [])}
    return {}


# ------------------------------------------------------------------- build ----
def build(diag):
    out, ahead = [], []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
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

        # regress toward average until recent games justify trusting the ratings
        per_team = recent_form(results)
        trust = min(1.0, per_team / GAMES_TO_TRUST.get(league, 10))
        keep = 1.0 - MAX_SHRINK * (1.0 - trust)
        rating = {t: v * keep for t, v in model["rating"].items()}
        # shrinking the ratings widens the honest error band by the same token
        sd_margin = (model["sd_margin"] ** 2 + ((1 - keep) * model["sd_margin"]) ** 2) ** 0.5
        diag.append(f"{league}: fit on {model['games']} games, hfa "
                    f"{model['hfa']:.2f}, sd margin {sd_margin:.2f}, "
                    f"{per_team:.1f} games per team in the last {RECENT_WINDOW} "
                    f"days — keeping {keep * 100:.0f}% of the fitted ratings")

        no_rating = no_h2h = too_far = 0
        for ev in fanduel_lines(sport_key, diag):
            home, away = ev.get("home_team"), ev.get("away_team")
            books = ev.get("bookmakers") or []
            if not (home and away and books):
                continue
            start = ev.get("commence_time") or ""
            try:
                when = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (now - timedelta(hours=6) <= when <= cutoff):
                too_far += 1
                if cutoff < when <= horizon:
                    fd_a = books[0]
                    h2h_a = market(fd_a, "h2h")
                    spr_a = market(fd_a, "spreads")
                    tot_a = market(fd_a, "totals")
                    if home in h2h_a and away in h2h_a:
                        ahead.append({
                            "sport": league, "home": home, "away": away,
                            "time": ev.get("commence_time"),
                            "mlHome": h2h_a[home].get("price"),
                            "mlAway": h2h_a[away].get("price"),
                            "spread": spr_a.get(home, {}).get("point"),
                            "total": tot_a.get("Over", {}).get("point"),
                        })
                continue
            fd = books[0]
            h2h, spreads, totals = market(fd, "h2h"), market(fd, "spreads"), market(fd, "totals")
            if home not in h2h or away not in h2h:
                no_h2h += 1
                continue
            if home not in rating or away not in rating:
                no_rating += 1
                continue

            g = {
                "sport": league, "home": home, "away": away,
                "time": ev.get("commence_time"),
                "homeRtg": round(rating[home], 3),
                "awayRtg": round(rating[away], 3),
                "hfa": round(model["hfa"], 3),
                "sdMargin": round(sd_margin, 3),
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
        diag.append(f"{league}: {priced} priced, {too_far} outside the "
                    f"{DAYS_AHEAD}-day window, {no_rating} with no rating, "
                    f"{no_h2h} with no FanDuel moneyline")
        print(f"  {priced} games priced")
    ahead.sort(key=lambda g: g["time"] or "")
    return out, ahead


def main():
    diag = []
    games, ahead = build(diag)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "adjusted-margin ratings fit jointly with home field, regressed "
                 "toward average unless the league has played recently; normal "
                 "margin and total distributions; half-point lines only; games "
                 f"starting within {DAYS_AHEAD} days",
        "days_ahead": DAYS_AHEAD,
        "lookahead_days": LOOKAHEAD_DAYS,
        "diagnostics": diag,
        "games": games,
        "lookahead": ahead,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"wrote {OUT_PATH} — {len(games)} games, {len(ahead)} in the lookahead")


if __name__ == "__main__":
    main()
