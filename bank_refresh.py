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
    times (PROPS_RUNS): 4 over/under markets + 4 milestone ladders = 8 credits
    per game
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
PROP_MARKETS = {   # regular over/unders, then the milestone ladders (150+ yards, 10+ threes)
    "americanfootball_nfl": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,"
                            "player_pass_yds_alternate,player_rush_yds_alternate,"
                            "player_reception_yds_alternate,player_receptions_alternate,player_anytime_td",
    "basketball_nba": "player_points,player_rebounds,player_assists,player_threes,"
                      "player_points_alternate,player_rebounds_alternate,"
                      "player_assists_alternate,player_threes_alternate",
}

# Refresh times in EASTERN time: (minute, hour, weekday "*"/"6"=Sat/"0"=Sun,
# sports, hours ahead to look, buy player props?). Props are bought only at the
# last-look times, for games starting within that run's window.
LOCAL_PLAN = [
    (5, 11, "*", ALL, 12, False),                        # daily 11:05am - college + early slates
    (50, 17, "*", ALL, 3, True),                         # daily 5:50pm - NBA/NCAAB 7:00-7:30 tips
    (45, 18, "*", ALL, 3, True),                         # daily 6:45pm - lineups, NFL night-game inactives
    (30, 9, "6", ALL, 6, False),                         # Sat 9:30am  - college football morning
    (20, 14, "6", ALL, 4, False),                        # Sat 2:20pm  - before 3:30 kickoffs
    (45, 14, "6", ALL, 4, False),                        # Sat 2:45pm  - before 3:30 kickoffs (backup)
    (45, 11, "0", ALL, 3, True),                         # Sun 11:45am - NFL inactives for 1pm games
    (30, 12, "0", ALL, 3, False),                        # Sun 12:30pm - last look before 1pm
    (52, 14, "0", ALL, 3, True),                         # Sun 2:52pm  - NFL inactives for 4pm games
    (10, 18, "0", ["americanfootball_ncaaf"], 168, False),  # Sun 6:10pm - next week's college openers
]
# GitHub only schedules in UTC, so every time is listed twice: once for daylight
# time (Eastern = UTC-4, Mar-Nov) and once for standard time (UTC-5, Nov-Mar).
# Each run checks which one is in effect today and the other one does nothing.
PLAN = {}
for _m, _h, _dow, _sports, _hours, _props in LOCAL_PLAN:
    for _off in (4, 5):
        _cron = f"{_m} {_h + _off} * * {_dow}"
        assert _cron not in PLAN, f"schedule clash at {_cron}"
        PLAN[_cron] = (_sports, _hours, _props, _off)
MANUAL = (ALL, 12, True, None)


def eastern_offset(now: datetime) -> int:
    """4 during daylight time, 5 during standard time."""
    from zoneinfo import ZoneInfo
    return int(-now.astimezone(ZoneInfo("America/New_York")).utcoffset().total_seconds() // 3600)


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
    sports, hours, want_props, offset = PLAN.get(schedule, MANUAL)
    if offset is not None and offset != eastern_offset(now):
        return [f"{schedule} is the {'daylight' if offset == 4 else 'standard'}-time copy of this run; "
                "the other copy runs today instead. Nothing bought."]
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
                 f" | milestone greens {out['milestoneCounts']['green']}"
                 f" | markets: {', '.join(out['markets'])}")
    return notes


if __name__ == "__main__":
    for line in main():
        print(line)
