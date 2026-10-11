"""Bank Mode: think like the house.

Reads the multi-book odds (market_odds.json from the engine's run, plus
bank_odds.json from Bank Mode's own refresh, whichever is newer) and writes
bank_mode.json for the Bank tab. It never touches the engine's picks.

Markets covered: moneyline, spread, total (over/under), and player props
(over/under lines only - one-sided props like "anytime TD" have no other side
to take the cut out of, so they are skipped).

For every FanDuel market on a game that has not started:

  1. Fair price  - the sharpest book's two prices with the cut taken out
                   (Pinnacle first, then LowVig, then BetOnline). If none of
                   them has the same number, the average of every book that does.
  2. House cut   - how much FanDuel builds in on that market (its "hold").
  3. Light       - GREEN  FanDuel's price beats the fair price by at least
                          GREEN_EV, a sharp book set the fair price, and its line
                          is fresh. The bank priced it wrong.
                          Player props: sharp books often skip props, so a prop
                          can also go green off an average of PROP_MIN_BOOKS or
                          more books, but only with the stricter PROP_AVG_EV edge.
                   YELLOW close to fair, or not enough books to confirm it.
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
BANK_ODDS_PATH = os.environ.get("BANK_ODDS_PATH", "bank_odds.json")
OUT_PATH = os.environ.get("BANK_MODE_PATH", "bank_mode.json")
LOG_PATH = os.environ.get("BANK_LOG_PATH", "bank_log.json")

SHARP_BOOKS = ["pinnacle", "lowvig", "betonlineag"]   # best first
BOOK_NAMES = {"pinnacle": "Pinnacle", "lowvig": "LowVig", "betonlineag": "BetOnline"}
GREEN_EV = 0.02        # FanDuel must pay at least 2% more than fair
YELLOW_FLOOR = -0.01   # within 1% of fair counts as "close to fair"
CAP_EV = 0.10          # more than 10% better than fair = data problem, not a gift
STALE_MINUTES = 90     # sharp line older than FanDuel's by more than this = don't trust it
PROP_MIN_BOOKS = 4     # a prop with no sharp line needs this many other books...
PROP_AVG_EV = 0.03     # ...and this much edge to go green
PROPS_KEEP_HOURS = 12  # props bought by Bank Mode are used for this long
MARKETS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
PROP_NAMES = {
    "player_pass_yds": "Passing yards", "player_rush_yds": "Rushing yards",
    "player_reception_yds": "Receiving yards", "player_receptions": "Receptions",
    "player_points": "Points", "player_rebounds": "Rebounds",
    "player_assists": "Assists", "player_threes": "Threes made",
}
SPORT_NAMES = {"americanfootball_nfl": "NFL", "americanfootball_ncaaf": "NCAAF",
               "basketball_nba": "NBA", "basketball_ncaab": "NCAAB"}

# Milestones ("Bijan Robinson 150+ rushing yards", "Steph Curry 10+ threes").
# These are one-sided ladders: there is no "under" to take the cut out of, so
# instead FanDuel is compared with every other book's price on the SAME
# milestone. Books price these close together, so FanDuel paying far more than
# the best other book is a real outlier.
MILESTONE_NAMES = {
    "player_pass_yds_alternate": "passing yards", "player_rush_yds_alternate": "rushing yards",
    "player_reception_yds_alternate": "receiving yards", "player_receptions_alternate": "receptions",
    "player_points_alternate": "points", "player_rebounds_alternate": "rebounds",
    "player_assists_alternate": "assists", "player_threes_alternate": "threes",
}
MILESTONE_MIN_BOOKS = 4    # other books that must post the same milestone
MILESTONE_GREEN = 0.20     # FanDuel's payout at least 20% above the best other book
MILESTONE_YELLOW = 0.05    # 5-20% above: worth a look, not a bet


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


def market_label(key: str) -> str | None:
    return MARKETS.get(key) or PROP_NAMES.get(key)


# --------------------------------------------------------------- markets ----
def _find(book: dict | None, key: str) -> dict | None:
    for m in (book or {}).get("markets") or []:
        if m.get("key") == key:
            return m
    return None


def _market(book: dict | None, key: str) -> list[dict] | None:
    """Two-way game market (moneyline / spread / total)."""
    m = _find(book, key)
    outs = (m or {}).get("outcomes") or []
    return outs if len(outs) == 2 else None


def _updated(book: dict | None, key: str) -> str | None:
    m = _find(book, key)
    return (m or {}).get("last_update") or (book or {}).get("last_update")


def prop_pairs(book: dict | None, key: str) -> dict:
    """Player prop over/under pairs: {(player, line): [over, under]}."""
    m = _find(book, key)
    groups: dict = {}
    for o in (m or {}).get("outcomes") or []:
        groups.setdefault((o.get("description"), o.get("point")), []).append(o)
    out = {}
    for pk, g in groups.items():
        by = {o["name"]: o for o in g}
        if len(g) == 2 and set(by) == {"Over", "Under"} and pk[0]:
            out[pk] = [by["Over"], by["Under"]]
    return out


def _pair(book: dict | None, key: str, pk) -> list[dict] | None:
    """The two outcomes of one market: a game market (pk None) or one player's line."""
    if pk is None:
        return _market(book, key)
    return prop_pairs(book, key).get(pk)


def _same_number(a: list[dict], b: list[dict]) -> bool:
    """Spreads and totals only compare when both books hang the same number."""
    pa = {o["name"]: o.get("point") for o in a}
    pb = {o["name"]: o.get("point") for o in b}
    return pa == pb


def _ordered(outs: list[dict], names: list[str]) -> list[dict] | None:
    by = {o["name"]: o for o in outs}
    if not all(n in by for n in names):
        return None
    return [by[n] for n in names]


def fair_line(event: dict, key: str, fd_outs: list[dict], pk=None) -> dict | None:
    """Fair probabilities for FanDuel's two outcomes, from the sharpest matching book."""
    names = [o["name"] for o in fd_outs]
    books = {b["key"]: b for b in event.get("bookmakers") or []}
    for bk in SHARP_BOOKS:
        b = books.get(bk)
        outs = _pair(b, key, pk) if b else None
        outs = _ordered(outs, names) if outs else None
        if outs and _same_number(outs, fd_outs):
            probs, hold = no_vig([o["price"] for o in outs])
            return {"probs": probs, "source": BOOK_NAMES[bk], "sharp": True, "books": 1,
                    "sharpHold": hold, "updated": _updated(b, key)}
    # No sharp book on the same number: average every other book that matches.
    sets = []
    for bk, b in books.items():
        if bk == "fanduel":
            continue
        outs = _pair(b, key, pk)
        outs = _ordered(outs, names) if outs else None
        if outs and _same_number(outs, fd_outs):
            sets.append(no_vig([o["price"] for o in outs])[0])
    if len(sets) < 2:
        return None
    probs = [sum(s[i] for s in sets) / len(sets) for i in range(2)]
    return {"probs": probs, "source": f"{len(sets)}-book average", "sharp": False,
            "books": len(sets), "sharpHold": None, "updated": None}


def light_for(ev: float, sharp: bool, stale: bool, prop_books: int = 0) -> tuple[str, str]:
    if ev > CAP_EV:
        return "check", "Too good to be true. Likely a stale or bad line, so it is not bet."
    if ev >= GREEN_EV and sharp and not stale:
        return "green", "FanDuel pays more than the fair price. The bank priced this one wrong."
    if ev >= GREEN_EV and sharp and stale:
        return "yellow", "Looks good, but the sharp line is old. Re-check before betting."
    if ev >= PROP_AVG_EV and prop_books >= PROP_MIN_BOOKS:
        return "green", (f"FanDuel pays more than {prop_books} other books agree is fair. "
                         "The bank priced this prop wrong.")
    if ev >= GREEN_EV:
        return "yellow", "Looks good, but not enough books posted this number to confirm it."
    if ev >= YELLOW_FLOOR:
        return "yellow", "Close to fair. No real edge for you or the house."
    return "red", "The house has the edge here. This is the cut you would pay."


def _side_label(key: str, pk, o: dict) -> tuple[str, float | None]:
    """What the side is called on the tab, and the number shown after it."""
    if pk is not None:                      # player prop: "Jalen Hurts Over 215.5"
        return f'{pk[0]} {o["name"]} {o.get("point")}', None
    if key == "totals":                     # total: "Over 47.5"
        return f'{o["name"]} {o.get("point")}', None
    return o["name"], o.get("point")        # moneyline / spread: team (+ spread)


def _fd_markets(fd: dict):
    """Every two-way FanDuel market: (key, pk, outcomes)."""
    for m in fd.get("markets") or []:
        key = m.get("key")
        if key in MARKETS:
            outs = _market(fd, key)
            if outs:
                yield key, None, outs
        elif key in PROP_NAMES:
            for pk, outs in prop_pairs(fd, key).items():
                yield key, pk, outs


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
            for key, pk, fd_outs in _fd_markets(fd):
                fair = fair_line(ev, key, fd_outs, pk)
                _, fd_hold = no_vig([o["price"] for o in fd_outs])
                fd_upd = _ts(_updated(fd, key))
                sh_upd = _ts(fair["updated"]) if fair else None
                stale = bool(fd_upd and sh_upd and (fd_upd - sh_upd).total_seconds() > STALE_MINUTES * 60)
                prop_books = fair["books"] if (fair and pk is not None and not fair["sharp"]) else 0
                sides = []
                for i, o in enumerate(fd_outs):
                    dec = american_to_decimal(o["price"])
                    team, point = _side_label(key, pk, o)
                    side = {"team": team, "point": point, "price": o["price"],
                            "impliedPct": round(100 / dec, 1), "name": o["name"], "line": o.get("point")}
                    if fair:
                        p = fair["probs"][i]
                        e = p * dec - 1
                        light, why = light_for(e, fair["sharp"], stale, prop_books)
                        side.update({"fairPct": round(p * 100, 1),
                                     "fairPrice": decimal_to_american(1 / p),
                                     "evPct": round(e * 100, 2), "light": light, "why": why})
                    else:
                        side.update({"fairPct": None, "fairPrice": None, "evPct": None,
                                     "light": "none", "why": "No other book has this number yet."})
                    sides.append(side)
                shaded = min(sides, key=lambda s: s["evPct"])["team"] if fair else None
                rid = f'{ev["id"]}|{key}' + (f"|{pk[0]}|{pk[1]}" if pk else "")
                rows.append({
                    "id": rid, "eventId": ev["id"], "marketKey": key,
                    "player": pk[0] if pk else None,
                    "sport": SPORT_NAMES.get(sport_key, sport_key), "time": ev["commence_time"],
                    "game": f'{ev["away_team"]} at {ev["home_team"]}', "market": market_label(key),
                    "isProp": pk is not None,
                    "fdHoldPct": round(fd_hold * 100, 2),
                    "fairSource": fair["source"] if fair else None,
                    "sharpHoldPct": round(fair["sharpHold"] * 100, 2) if fair and fair["sharpHold"] is not None else None,
                    "stale": stale, "shadedSide": shaded, "sides": sides,
                })
    rows.sort(key=lambda r: (r["time"], r["game"], r["isProp"], r["market"], r["player"] or ""))
    return rows


def _milestone_label(point) -> str:
    """149.5 -> '150+', 9.5 -> '10+'; a whole number stays 'over 150'."""
    p = float(point)
    return f"{int(p + 0.5)}+" if abs(p % 1 - 0.5) < 1e-9 else f"over {point:g}"


def _overs(book: dict | None, key: str) -> dict:
    """One book's milestone ladder: {(player, point): over price}."""
    m = _find(book, key)
    return {(o.get("description"), o.get("point")): o["price"]
            for o in (m or {}).get("outcomes") or []
            if o.get("name") == "Over" and o.get("description") and o.get("point") is not None}


def build_milestones(odds: dict, now: datetime) -> list[dict]:
    rows = []
    for sport_key, events in (odds.get("sports") or {}).items():
        for ev in events:
            start = _ts(ev.get("commence_time"))
            if not start or start <= now:
                continue
            books = {b["key"]: b for b in ev.get("bookmakers") or []}
            fd = books.get("fanduel")
            if not fd:
                continue
            for key, stat in MILESTONE_NAMES.items():
                fd_ladder = _overs(fd, key)
                if not fd_ladder:
                    continue
                others = {bk: _overs(b, key) for bk, b in books.items() if bk != "fanduel"}
                for (player, point), price in fd_ladder.items():
                    rivals = {bk: lad[(player, point)] for bk, lad in others.items() if (player, point) in lad}
                    label = f"{player} {_milestone_label(point)} {stat}"
                    side = {"team": label, "point": None, "price": price, "name": "Over", "line": point,
                            "impliedPct": round(100 / american_to_decimal(price), 1),
                            "fairPct": None, "evPct": None, "booksCompared": len(rivals)}
                    if len(rivals) < MILESTONE_MIN_BOOKS:
                        side.update({"fairPrice": None, "outlierPct": None, "light": "none",
                                     "why": f"Only {len(rivals)} other book(s) post this milestone; "
                                            f"need {MILESTONE_MIN_BOOKS} to compare."})
                        source = None
                    else:
                        best_bk, best = max(rivals.items(), key=lambda kv: american_to_decimal(kv[1]))
                        fd_profit = american_to_decimal(price) - 1
                        best_profit = american_to_decimal(best) - 1
                        outlier = fd_profit / best_profit - 1
                        avg_prob = sum(1 / american_to_decimal(p) for p in rivals.values()) / len(rivals)
                        if outlier >= MILESTONE_GREEN:
                            light = "green"
                            why = (f"FanDuel pays {outlier * 100:.0f}% more than the best of {len(rivals)} "
                                   f"other books. FanDuel is way out of line on this milestone.")
                        elif outlier >= MILESTONE_YELLOW:
                            light, why = "yellow", (f"FanDuel pays {outlier * 100:.0f}% more than the best "
                                                    f"other book. A little generous, not enough to bet.")
                        else:
                            light, why = "red", "Other books pay the same or more. No FanDuel mistake here."
                        side.update({"fairPrice": best, "bestBook": best_bk,
                                     "avgOtherPrice": decimal_to_american(1 / avg_prob),
                                     "outlierPct": round(outlier * 100, 1), "light": light, "why": why})
                        source = f"Best of {len(rivals)} books"
                    rows.append({
                        "id": f'{ev["id"]}|{key}|{player}|{point}', "eventId": ev["id"], "marketKey": key,
                        "player": player, "sport": SPORT_NAMES.get(sport_key, sport_key),
                        "time": ev["commence_time"], "game": f'{ev["away_team"]} at {ev["home_team"]}',
                        "market": f"Milestone {stat}", "isProp": True, "isMilestone": True,
                        "fdHoldPct": None, "fairSource": source, "sharpHoldPct": None,
                        "stale": False, "shadedSide": None, "sides": [side],
                    })
    rows.sort(key=lambda r: (r["time"], r["game"], r["market"], r["player"], r["sides"][0]["line"]))
    return rows


# ---------------------------------------------------------- odds sources ----
def _merge_markets(primary: list, extra: list) -> None:
    """Add markets (totals, props) that `extra` has and `primary` lacks, per event and book."""
    by_id = {e["id"]: e for e in primary}
    for ev in extra:
        target = by_id.get(ev["id"])
        if target is None:
            continue
        tbooks = {b["key"]: b for b in target.setdefault("bookmakers", [])}
        for b in ev.get("bookmakers") or []:
            tb = tbooks.get(b["key"])
            if tb is None:
                tb = {"key": b["key"], "title": b.get("title"), "last_update": b.get("last_update"),
                      "markets": []}
                target["bookmakers"].append(tb)
                tbooks[b["key"]] = tb
            have = {m["key"] for m in tb.get("markets") or []}
            for m in b.get("markets") or []:
                if m["key"] not in have:
                    m = dict(m)
                    m.setdefault("last_update", b.get("last_update"))
                    tb.setdefault("markets", []).append(m)


def combine(bank: dict, engine: dict, now: datetime | None = None) -> dict:
    """Newest odds per sport (Bank Mode's refresh or the engine's run), plus the
    markets only Bank Mode buys (totals, player props) layered on top."""
    import copy
    now = now or datetime.now(timezone.utc)
    out, newest = {}, None
    eng_at = engine.get("generated_at")
    bsports, esports = bank.get("sports", {}), engine.get("sports", {})
    for sport in set(bsports) | set(esports):
        b = bsports.get(sport) or {}
        if b.get("events") is not None and (not eng_at or b.get("fetched_at", "") >= eng_at):
            events, at, other = copy.deepcopy(b["events"]), b["fetched_at"], esports.get(sport) or []
        else:
            events, at, other = copy.deepcopy(esports.get(sport) or []), eng_at, b.get("events") or []
        _merge_markets(events, other)          # e.g. bank totals onto a newer engine pull
        p = b.get("props") or {}
        p_at = _ts(p.get("fetched_at"))
        if p_at and (now - p_at).total_seconds() < PROPS_KEEP_HOURS * 3600:
            _merge_markets(events, list((p.get("events") or {}).values()))
        out[sport] = events
        if events and at and (newest is None or at > newest):
            newest = at
    return {"generated_at": newest, "sports": out}


def load_odds(now: datetime) -> dict:
    def load(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    return combine(load(BANK_ODDS_PATH), load(ODDS_PATH), now)


# ------------------------------------------------------------------- log ----
def _row_by_id(rows: list[dict]) -> dict:
    return {r["id"]: r for r in rows}


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
                     "time": r["time"], "market": r["market"], "marketKey": r.get("marketKey"),
                     "player": r.get("player"), "name": s.get("name"), "line": s.get("line"),
                     "team": s["team"], "point": s["point"],
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
            key = e.get("marketKey") or mkey.get(e["market"])
            fd = next((b for b in (ev or {}).get("bookmakers") or [] if b.get("key") == "fanduel"), None)
            fair, idx = None, None
            if ev and fd and (key in PROP_NAMES or key == "totals"):
                # props and totals: judge the close on OUR line (same player, same number)
                pk = (e.get("player"), e.get("line")) if key in PROP_NAMES else None
                if pk is not None:
                    ours = [{"name": "Over", "point": e.get("line"), "price": -110},
                            {"name": "Under", "point": e.get("line"), "price": -110}]
                else:
                    outs = _market(fd, key)
                    ours = [dict(o, point=e.get("line")) for o in outs] if outs else None
                if ours:
                    fair = fair_line(ev, key, ours, pk)
                    idx = [o["name"] for o in ours].index(e.get("name") or "Over")
            elif ev and fd:
                outs = _market(fd, key)
                if outs:
                    # judge the close against OUR number, even if FanDuel has moved off it
                    ours = [dict(o) for o in outs]
                    for o in ours:
                        if o["name"] == e["team"]:
                            o["point"] = e["point"]
                        elif e["point"] is not None:
                            o["point"] = -e["point"]
                    fair = fair_line(ev, key, ours)
                    idx = [o["name"] for o in ours].index(e["team"])
            if fair and fair["sharp"] and idx is not None:
                e["closeFairPct"] = round(fair["probs"][idx] * 100, 1)
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
def main(now: datetime | None = None, odds: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    if odds is None:
        odds = load_odds(now)
    log = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            log = json.load(f)
    rows = build_rows(odds, now)
    log = update_log(log, rows, odds, now, stamp)
    milestones = build_milestones(odds, now)

    game_rows = [r for r in rows if not r["isProp"]]
    holds = [r["fdHoldPct"] / 100 for r in game_rows]
    avg_hold = sum(holds) / len(holds) if holds else 100 / 2100   # -110/-110 when empty
    counts = {k: sum(1 for r in rows for s in r["sides"] if s["light"] == k)
              for k in ("green", "yellow", "red", "check", "none")}
    m_counts = {k: sum(1 for r in milestones if r["sides"][0]["light"] == k)
                for k in ("green", "yellow", "red", "none")}
    rows = rows + milestones
    out = {
        "generated_at": stamp, "odds_as_of": odds.get("generated_at"),
        "rules": {"greenEvPct": GREEN_EV * 100, "capEvPct": CAP_EV * 100,
                  "staleMinutes": STALE_MINUTES, "sharpBooks": [BOOK_NAMES[b] for b in SHARP_BOOKS],
                  "propMinBooks": PROP_MIN_BOOKS, "propAvgEvPct": PROP_AVG_EV * 100,
                  "milestoneMinBooks": MILESTONE_MIN_BOOKS, "milestoneGreenPct": MILESTONE_GREEN * 100},
        "counts": counts,
        "milestoneCounts": m_counts,
        "markets": sorted({r["market"] for r in rows}),
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
