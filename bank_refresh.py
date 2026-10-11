"""Bank Mode's own odds refresh. Separate from the engine's weekly slate.

Runs on its own schedule (.github/workflows/bank-mode.yml) at the times
FanDuel is most likely to lag the sharp books: NFL inactives, college
football Saturday mornings, NBA and college basketball injury/lineup news.

It never touches the engine's files (edge_slate.json, picks_log.json,
market_odds.json, results.json). It only writes bank_odds.json, then rebuilds
bank_mode.json and bank_log.json.

Credits: before buying any odds it asks The Odds API which games are coming
up, which is free. It only buys a sport (2 credits: moneyline + spread, one
region of 10 books) when that sport has a game starting soon.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

import bank_mode

BANK_ODDS_PATH = os.environ.get("BANK_ODDS_PATH", "bank_odds.json")
ENGINE_ODDS_PATH = os.environ.get("MARKET_ODDS_PATH", "market_odds.json")
COMBINED_PATH = os.environ.get("BANK_COMBINED_PATH", "/tmp/bank_combined_odds.json")
BOOKS = ("fanduel,draftkings,betmgm,williamhill_us,betrivers,"
         "espnbet,fanatics,pinnacle,betonlineag,lowvig")
ODDS = "https://api.the-odds-api.com/v4/sports/{key}/odds"
EVENTS = "https://api.the-odds-api.com/v4/sports/{key}/events"   # free: no credits
ALL = ["americanfootball_nfl", "americanfootball_ncaaf", "basketball_nba", "basketball_ncaab"]

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


def keys() -> list[str]:
    return [k for k in (os.environ.get("ODDS_API_KEY"), os.environ.get("ODDS_API_KEY_BACKUP")) if k]


def games_soon(sport: str, hours: int, key: str, now: datetime) -> int:
    """How many games start in the next `hours`. Free call."""
    r = requests.get(EVENTS.format(key=sport), timeout=30, params={
        "apiKey": key,
        "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    if r.status_code != 200:
        raise RuntimeError(f"events HTTP {r.status_code} {r.text[:120]}")
    return len(r.json())


def buy_odds(sport: str, api_keys: list[str], notes: list[str]) -> list | None:
    for i, key in enumerate(api_keys):
        r = requests.get(ODDS.format(key=sport), timeout=30, params={
            "apiKey": key, "regions": "us", "oddsFormat": "american",
            "markets": "h2h,spreads", "bookmakers": BOOKS})
        if r.status_code == 200:
            notes.append(f"{sport}: bought {len(r.json())} games"
                         f" ({r.headers.get('x-requests-remaining', '?')} credits left)")
            return r.json()
        notes.append(f"{sport}: key {i + 1} HTTP {r.status_code} {r.text[:120]}")
    return None


def combine(bank: dict, engine: dict) -> dict:
    """For each sport, use whichever odds are newer: this refresh or the engine's run."""
    out, newest = {}, None
    eng_at = engine.get("generated_at")
    for sport in set(bank.get("sports", {})) | set(engine.get("sports", {})):
        b = bank.get("sports", {}).get(sport)
        if b and (not eng_at or b["fetched_at"] >= eng_at):
            out[sport], at = b["events"], b["fetched_at"]
        else:
            out[sport], at = engine.get("sports", {}).get(sport, []), eng_at
        if out[sport] and at and (newest is None or at > newest):
            newest = at
    return {"generated_at": newest, "sports": out}


def load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    sports, hours = PLAN.get(os.environ.get("SCHEDULE", ""), MANUAL)
    api_keys, notes = keys(), []
    bank = load(BANK_ODDS_PATH) or {"sports": {}}
    bank.setdefault("sports", {})
    if not api_keys:
        notes.append("no ODDS_API_KEY set - nothing bought")
    for sport in sports:
        if not api_keys:
            break
        try:
            n = games_soon(sport, hours, api_keys[0], now)
        except Exception as e:
            notes.append(f"{sport}: could not check schedule - {e}")
            continue
        if n == 0:
            notes.append(f"{sport}: no games in the next {hours}h - skipped (0 credits)")
            continue
        events = buy_odds(sport, api_keys, notes)
        if events is not None:
            bank["sports"][sport] = {"fetched_at": now.isoformat(timespec="seconds"), "events": events}
    with open(BANK_ODDS_PATH, "w") as f:
        json.dump(bank, f, separators=(",", ":"))

    with open(COMBINED_PATH, "w") as f:
        json.dump(combine(bank, load(ENGINE_ODDS_PATH)), f, separators=(",", ":"))
    bank_mode.ODDS_PATH = COMBINED_PATH
    out = bank_mode.main(now)
    c = out["counts"]
    notes.append(f"Bank Mode: green {c['green']} yellow {c['yellow']} red {c['red']} check {c['check']}")
    return notes


if __name__ == "__main__":
    for line in main():
        print(line)
