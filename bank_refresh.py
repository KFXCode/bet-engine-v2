"""Bank Mode's own odds refresh. Separate from the engine's weekly slate.

Runs on its own schedule (.github/workflows/bank-mode.yml) at the times
FanDuel is most likely to lag the sharp books: NFL inactives, college
football Saturday mornings, NBA and college basketball injury/lineup news.

It never touches the engine's files (edge_slate.json, picks_log.json,
market_odds.json, results.json). It only writes bank_odds.json, then rebuilds
bank_mode.json and bank_log.json.

What it buys (The Odds API, one region of 10 books):
  - game lines for a sport with a game starting soon: moneyline, spread and
    total = 3 credits per sport
  - player props for NFL and NBA games starting soon, only at the "last look"
    times (PROPS_RUNS): 4 prop markets = 4 credits per game
Before buying anything it asks which games are coming up, which is free.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

import bank_mode

BANK_ODDS_PATH = os.environ.get("BANK_ODDS_PATH", "bank_odds.json")
ENGINE_ODDS_PATH = os.environ.get("MARKET_ODDS_PATH", "market_odds.json")
BOOKS = ("fanduel,draftkings,betmgm,williamhill_us,betrivers,"
         "espnbet,fanatics,pinnacle,betonlineag,lowvig")
GAME_MARKETS = "h2h,spreads,totals"
ODDS = "https://api.the-odds-api.com/v4/sports/{key}/odds"
EVENT_ODDS = "https://api.the-odds-api.com/v4/sports/{key}/events/{event}/odds"
EVENTS = "https://api.the-odds-api.com/v4/sports/{key}/events"   # free: no credits
ALL = ["americanfootball_nfl", "americanfootball_ncaaf", "basketball_nba", "basketball_ncaab"]
PROP_MARKETS = {
    "americanfootball_nfl": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions",
    "basketball_nba": "player_points,player_rebounds,player_assists,player_threes",
}

# Which sports to check and how far ahead to look, per scheduled time (UTC).
# Eastern times are for daylight time (Mar-Nov); after the November clock
# change every run lands one hour earlier in Eastern.
PLAN = {
    "5 15 * * *":  (ALL, 12),                         # daily 11:05am - college + early slates
    "0 22 * * *":  (ALL, 6),                          # daily 6:00pm - NBA/NCAAB injury news
    "45 22 * * *": (ALL, 3),                          # daily 6:45pm - lineups, NFL night-game inactives
    "30 13 * * 6": (ALL, 6),                          # Sat 9:30am  - college football morning
    "45 18 * * 6": (ALL, 4),                          # Sat 2:45pm  - before 3:30 kickoffs
    "45 15 * * 0": (ALL, 3),                          # Sun 11:45am - NFL inactives for 1pm games
    "30 16 * * 0": (ALL, 3),                          # Sun 12:30pm - last look before 1pm
    "0 19 * * 0":  (ALL, 3),                          # Sun 3:00pm  - NFL inactives for 4pm games
    "10 22 * * 0": (["americanfootball_ncaaf"], 168), # Sun 6:10pm  - next week's college openers
}
MANUAL = (ALL, 12)
# Player props are bought only at these last-look times (and on a manual run),
# for games starting within that run's window.
PROPS_RUNS = {"45 22 * * *", "45 15 * * 0", "0 19 * * 0", ""}


def keys() -> list[str]:
    return [k for k in (os.environ.get("ODDS_API_KEY"), os.environ.get("ODDS_API_KEY_BACKUP")) if k]


def games_soon(sport: str, hours: int, key: str, now: datetime) -> list[dict]:
    """Games starting in the next `hours`. Free call."""
    r = requests.get(EVENTS.format(key=sport), timeout=30, params={
        "apiKey": key,
        "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    if r.status_code != 200:
        raise RuntimeError(f"events HTTP {r.status_code} {r.text[:120]}")
    return r.json()


def _get(url: str, params: dict, api_keys: list[str], notes: list[str], label: str):
    for i, key in enumerate(api_keys):
        r = requests.get(url, timeout=30, params=dict(params, apiKey=key))
        if r.status_code == 200:
            return r.json(), r.headers.get("x-requests-remaining", "?")
        notes.append(f"{label}: key {i + 1} HTTP {r.status_code} {r.text[:120]}")
    return None, None


def buy_odds(sport: str, api_keys: list[str], notes: list[str]) -> list | None:
    data, left = _get(ODDS.format(key=sport), {"regions": "us", "oddsFormat": "american",
                                               "markets": GAME_MARKETS, "bookmakers": BOOKS},
                      api_keys, notes, sport)
    if data is not None:
        notes.append(f"{sport}: bought {len(data)} games ({left} credits left)")
    return data


def buy_props(sport: str, games: list[dict], api_keys: list[str], notes: list[str]) -> dict:
    out, left = {}, None
    for g in games:
        data, left = _get(EVENT_ODDS.format(key=sport, event=g["id"]),
                          {"regions": "us", "oddsFormat": "american",
                           "markets": PROP_MARKETS[sport], "bookmakers": BOOKS},
                          api_keys, notes, f"{sport} props {g.get('away_team')} at {g.get('home_team')}")
        if data:
            out[g["id"]] = data
    notes.append(f"{sport}: bought props for {len(out)} of {len(games)} games ({left} credits left)")
    return out


def load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    schedule = os.environ.get("SCHEDULE", "")
    sports, hours = PLAN.get(schedule, MANUAL)
    want_props = schedule in PROPS_RUNS
    api_keys, notes = keys(), []
    bank = load(BANK_ODDS_PATH) or {"sports": {}}
    bank.setdefault("sports", {})
    stamp = now.isoformat(timespec="seconds")
    if not api_keys:
        notes.append("no ODDS_API_KEY set - nothing bought")
    for sport in sports:
        if not api_keys:
            break
        try:
            soon = games_soon(sport, hours, api_keys[0], now)
        except Exception as e:
            notes.append(f"{sport}: could not check schedule - {e}")
            continue
        if not soon:
            notes.append(f"{sport}: no games in the next {hours}h - skipped (0 credits)")
            continue
        entry = bank["sports"].setdefault(sport, {})
        events = buy_odds(sport, api_keys, notes)
        if events is not None:
            entry.update({"fetched_at": stamp, "events": events})
        if want_props and sport in PROP_MARKETS:
            props = buy_props(sport, soon, api_keys, notes)
            if props:
                entry["props"] = {"fetched_at": stamp, "events": props}
    # drop props for games that already started
    for entry in bank["sports"].values():
        p = entry.get("props") or {}
        p["events"] = {k: v for k, v in (p.get("events") or {}).items()
                       if (bank_mode._ts(v.get("commence_time")) or now) > now}
    with open(BANK_ODDS_PATH, "w") as f:
        json.dump(bank, f, separators=(",", ":"))

    bank_mode.BANK_ODDS_PATH = BANK_ODDS_PATH
    bank_mode.ODDS_PATH = ENGINE_ODDS_PATH
    out = bank_mode.main(now)
    c = out["counts"]
    notes.append(f"Bank Mode: green {c['green']} yellow {c['yellow']} red {c['red']} check {c['check']}"
                 f" | markets: {', '.join(out['markets'])}")
    return notes


if __name__ == "__main__":
    for line in main():
        print(line)
