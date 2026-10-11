"""Bank Mode: think like the house.

Reads market_odds.json (the multi-book odds edge_slate.py already saves) and
writes bank_mode.json for the Bank tab. Spends no odds-API credits.

For every FanDuel moneyline and spread on a game that has not started:

  1. Fair price  - the sharpest book's two prices with the cut taken out
                   (Pinnacle first, then LowVig, then BetOnline). If none of
                   them has the same number, the average of every book that does.
  2. House cut   - how much FanDuel builds in on that market (its "hold").
  3. Light       - GREEN  FanDuel's price beats the sharp fair price by at least
                          GREEN_EV, a sharp book set the fair price, and its line
                          is fresh. The bank priced it wrong.
                   YELLOW close to fair, or only an all-books average to judge by.
                   RED    the house has the edge. This is where the cut comes from.
                   CHECK  an edge too big to believe (stale or bad data), not bet.
  4. Shaded side - the side FanDuel charges more on. That is the side the bank
                   wants you on; the other side gets the better price.

Every green light goes into bank_log.json with the price at the moment it lit.
Each later run, until kickoff, records the newest sharp fair price for that same
number, so the last one saved before the game starts is the closing price.
Closing line value (CLV) = how much better our price was than that close.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ODDS_PATH = os.environ.get("MARKET_ODDS_PATH", "market_odds.json")
OUT_PATH = os.environ.get("BANK_MODE_PATH", "bank_mode.json")
LOG_PATH = os.environ.get("BANK_LOG_PATH", "bank_log.json")

SHARP_BOOKS = ["pinnacle", "lowvig", "betonlineag"]   # best first
BOOK_NAMES = {"pinnacle": "Pinnacle", "lowvig": "LowVig", "betonlineag": "BetOnline"}
GREEN_EV = 0.02        # FanDuel must pay at least 2% more than fair
YELLOW_FLOOR = -0.01   # within 1% of fair counts as "close to fair"
CAP_EV = 0.10          # more than 10% better than fair = data problem, not a gift
STALE_MINUTES = 90     # sharp line older than FanDuel's by more than this = don't trust it
MARKETS = {"h2h": "Moneyline", "spreads": "Spread"}
SPORT_NAMES = {"americanfootball_nfl": "NFL", "americanfootball_ncaaf": "NCAAF",
               "basketball_nba": "NBA", "basketball_ncaab": "NCAAB"}


# ------------------------------------------------------------------ math ----
def american_to_decimal(price: float) -> float:
    price = float(price)
    return 1 + (price / 100 if price > 0 else 100 / -price)


def decimal_to_american(dec: float) -> int:
    if dec >= 2:
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))


def no_vig(prices: list[float]) -> tuple[list[float], float]:
    """Fair probabilities with the cut removed (scaled to add up to 100%), and the cut."""
    implied = [1 / american_to_decimal(p) for p in prices]
    total = sum(implied)
    return [x / total for x in implied], total - 1


def parlay_house_keep(hold: float, legs: int) -> float:
    """Share of a fair parlay payout the house keeps when every leg carries `hold`."""
    return 1 - (1 / (1 + hold)) ** legs


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------- markets ----
def _market(book: dict, key: str) -> list[dict] | None:
    for m in book.get("markets") or []:
        if m.get("key") == key:
            outs = m.get("outcomes") or []
            return outs if len(outs) == 2 else None
    return None


def _same_number(a: list[dict], b: list[dict]) -> bool:
    """Spreads only compare when both books hang the same number on each team."""
    pa = {o["name"]: o.get("point") for o in a}
    pb = {o["name"]: o.get("point") for o in b}
    return pa == pb


def _ordered(outs: list[dict], names: list[str]) -> list[dict] | None:
    by = {o["name"]: o for o in outs}
    if not all(n in by for n in names):
        return None
    return [by[n] for n in names]


def fair_line(event: dict, key: str, fd_outs: list[dict]) -> dict | None:
    """Fair probabilities for FanDuel's two outcomes, from the sharpest matching book."""
    names = [o["name"] for o in fd_outs]
    books = {b["key"]: b for b in event.get("bookmakers") or []}
    for bk in SHARP_BOOKS:
        b = books.get(bk)
        outs = _market(b, key) if b else None
        outs = _ordered(outs, names) if outs else None
        if outs and _same_number(outs, fd_outs):
            probs, hold = no_vig([o["price"] for o in outs])
            return {"probs": probs, "source": BOOK_NAMES[bk], "sharp": True,
                    "sharpHold": hold, "updated": b.get("last_update")}
    # No sharp book on the same number: average every other book that matches.
    sets = []
    for bk, b in books.items():
        if bk == "fanduel":
            continue
        outs = _market(b, key)
        outs = _ordered(outs, names) if outs else None
        if outs and _same_number(outs, fd_outs):
            sets.append(no_vig([o["price"] for o in outs])[0])
    if len(sets) < 2:
        return None
    probs = [sum(s[i] for s in sets) / len(sets) for i in range(2)]
    return {"probs": probs, "source": f"{len(sets)}-book average", "sharp": False,
            "sharpHold": None, "updated": None}


def light_for(ev: float, sharp: bool, stale: bool) -> tuple[str, str]:
    if ev > CAP_EV:
        return "check", "Too good to be true. Likely a stale or bad line, so it is not bet."
    if ev >= GREEN_EV and sharp and not stale:
        return "green", "FanDuel pays more than the fair price. The bank priced this one wrong."
    if ev >= GREEN_EV and sharp and stale:
        return "yellow", "Looks good, but the sharp line is old. Re-check before betting."
    if ev >= GREEN_EV:
        return "yellow", "Looks good, but no sharp book posted this number to confirm it."
    if ev >= YELLOW_FLOOR:
        return "yellow", "Close to fair. No real edge for you or the house."
    return "red", "The house has the edge here. This is the cut you would pay."


def build_rows(odds: dict, now: datetime) -> list[dict]:
    rows = []
    for sport_key, events in (odds.get("sports") or {}).items():
        for ev in events:
            start = _ts(ev.get("commence_time"))
            if not start or start <= now:
                continue
            fd = next((b for b in ev.get("bookmakers") or [] if b.get("key") == "fanduel"), None)
            if not fd:
                continue
            for key, label in MARKETS.items():
                fd_outs = _market(fd, key)
                if not fd_outs:
                    continue
                fair = fair_line(ev, key, fd_outs)
                _, fd_hold = no_vig([o["price"] for o in fd_outs])
                fd_upd, sh_upd = _ts(fd.get("last_update")), _ts(fair["updated"]) if fair else None
                stale = bool(fd_upd and sh_upd and (fd_upd - sh_upd).total_seconds() > STALE_MINUTES * 60)
                sides = []
                for i, o in enumerate(fd_outs):
                    dec = american_to_decimal(o["price"])
                    side = {"team": o["name"], "point": o.get("point"), "price": o["price"],
                            "impliedPct": round(100 / dec, 1)}
                    if fair:
                        p = fair["probs"][i]
                        e = p * dec - 1
                        light, why = light_for(e, fair["sharp"], stale)
                        side.update({"fairPct": round(p * 100, 1),
                                     "fairPrice": decimal_to_american(1 / p),
                                     "evPct": round(e * 100, 2), "light": light, "why": why})
                    else:
                        side.update({"fairPct": None, "fairPrice": None, "evPct": None,
                                     "light": "none", "why": "No other book has this number yet."})
                    sides.append(side)
                shaded = None
                if fair:
                    shaded = min(sides, key=lambda s: s["evPct"])["team"]
                rows.append({
                    "id": f'{ev["id"]}|{key}', "eventId": ev["id"],
                    "sport": SPORT_NAMES.get(sport_key, sport_key), "time": ev["commence_time"],
                    "game": f'{ev["away_team"]} at {ev["home_team"]}', "market": label,
                    "fdHoldPct": round(fd_hold * 100, 2),
                    "fairSource": fair["source"] if fair else None,
                    "sharpHoldPct": round(fair["sharpHold"] * 100, 2) if fair and fair["sharpHold"] is not None else None,
                    "stale": stale, "shadedSide": shaded, "sides": sides,
                })
    rows.sort(key=lambda r: (r["time"], r["game"], r["market"]))
    return rows


# ------------------------------------------------------------------- log ----
def update_log(log: list[dict], rows: list[dict], odds: dict, now: datetime, stamp: str) -> list[dict]:
    """Add new green lights; refresh the closing fair price on open entries."""
    by_key = {e["key"]: e for e in log}
    # 1. new green lights, at the price shown the moment they lit
    for r in rows:
        for s in r["sides"]:
            if s["light"] != "green":
                continue
            key = f'{r["id"]}|{s["team"]}|{s["point"]}'
            if key in by_key:
                continue
            entry = {"key": key, "eventId": r["eventId"], "sport": r["sport"], "game": r["game"],
                     "time": r["time"], "market": r["market"], "team": s["team"], "point": s["point"],
                     "flaggedAt": stamp, "price": s["price"], "fairPctAtFlag": s["fairPct"],
                     "evPctAtFlag": s["evPct"], "closeFairPct": s["fairPct"], "closeSource": r["fairSource"],
                     "closeSeenAt": stamp, "clvPct": None}
            log.append(entry)
            by_key[key] = entry
    # 2. before kickoff, keep the newest sharp fair price for the same number
    events = {ev["id"]: ev for evs in (odds.get("sports") or {}).values() for ev in evs}
    mkey = {v: k for k, v in MARKETS.items()}
    for e in log:
        start = _ts(e["time"])
        if start and start > now:
            ev = events.get(e["eventId"])
            fd = next((b for b in (ev or {}).get("bookmakers") or [] if b.get("key") == "fanduel"), None)
            outs = _market(fd, mkey[e["market"]]) if fd else None
            if ev and outs:
                # judge the close against OUR number, even if FanDuel has moved off it
                ours = [dict(o) for o in outs]
                for o in ours:
                    if o["name"] == e["team"]:
                        o["point"] = e["point"]
                    elif e["point"] is not None:
                        o["point"] = -e["point"]
                fair = fair_line(ev, mkey[e["market"]], ours)
                if fair and fair["sharp"]:
                    i = [o["name"] for o in ours].index(e["team"])
                    e["closeFairPct"] = round(fair["probs"][i] * 100, 1)
                    e["closeSource"] = fair["source"]
                    e["closeSeenAt"] = stamp
        if e.get("closeFairPct"):
            e["clvPct"] = round((american_to_decimal(e["price"]) * e["closeFairPct"] / 100 - 1) * 100, 2)
        e["closed"] = bool(start and start <= now)
    return log


def scoreboard(log: list[dict]) -> dict:
    closed = [e for e in log if e.get("closed") and e.get("clvPct") is not None]
    beat = [e for e in closed if e["clvPct"] > 0]
    return {"flagged": len(log), "closed": len(closed), "beatClose": len(beat),
            "beatClosePct": round(100 * len(beat) / len(closed), 1) if closed else None,
            "avgClvPct": round(sum(e["clvPct"] for e in closed) / len(closed), 2) if closed else None}


# ------------------------------------------------------------------ main ----
def main(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    with open(ODDS_PATH) as f:
        odds = json.load(f)
    log = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            log = json.load(f)
    rows = build_rows(odds, now)
    log = update_log(log, rows, odds, now, stamp)

    holds = [r["fdHoldPct"] / 100 for r in rows]
    avg_hold = sum(holds) / len(holds) if holds else 100 / 2100   # -110/-110 when empty
    counts = {k: sum(1 for r in rows for s in r["sides"] if s["light"] == k)
              for k in ("green", "yellow", "red", "check", "none")}
    out = {
        "generated_at": stamp, "odds_as_of": odds.get("generated_at"),
        "rules": {"greenEvPct": GREEN_EV * 100, "capEvPct": CAP_EV * 100,
                  "staleMinutes": STALE_MINUTES, "sharpBooks": [BOOK_NAMES[b] for b in SHARP_BOOKS]},
        "counts": counts,
        "avgFdHoldPct": round(avg_hold * 100, 2),
        "parlay": [{"legs": n, "houseKeepsPct": round(parlay_house_keep(avg_hold, n) * 100, 1)}
                   for n in range(1, 7)],
        "rows": rows,
        "scoreboard": scoreboard(log),
        "log": sorted(log, key=lambda e: e["flaggedAt"], reverse=True)[:60],
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=1)
    return out


if __name__ == "__main__":
    o = main()
    c = o["counts"]
    print(f'Bank Mode: {len(o["rows"])} markets | green {c["green"]} yellow {c["yellow"]} '
          f'red {c["red"]} check {c["check"]} | avg FanDuel cut {o["avgFdHoldPct"]}%')
