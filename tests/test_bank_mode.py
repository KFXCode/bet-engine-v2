from datetime import datetime, timezone

import bank_mode as bm

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)   # after kickoff


def book(key, home_price, away_price, home_pt=None, upd="2026-10-11T11:55:00Z"):
    def o(name, price, pt):
        d = {"name": name, "price": price}
        if pt is not None:
            d["point"] = pt
        return d
    mk = "spreads" if home_pt is not None else "h2h"
    away_pt = -home_pt if home_pt is not None else None
    return {"key": key, "last_update": upd, "markets": [
        {"key": mk, "outcomes": [o("Home", home_price, home_pt), o("Away", away_price, away_pt)]}]}


def odds(*books, start="2026-10-11T16:00:00Z"):
    return {"generated_at": "x", "sports": {"americanfootball_nfl": [{
        "id": "g1", "commence_time": start, "home_team": "Home", "away_team": "Away",
        "bookmakers": list(books)}]}}


def test_no_vig_and_hold():
    probs, hold = bm.no_vig([-110, -110])
    assert abs(probs[0] - 0.5) < 1e-9
    assert abs(hold - 0.047619) < 1e-5          # 104.76% - 100%


def test_parlay_keep_matches_doc():
    # 4 legs at -110/-110: pays +1228 vs fair +1500 -> house keeps ~17%
    assert round(bm.parlay_house_keep(1 / 21, 4) * 100) == 17


def test_green_when_fanduel_beats_sharp():
    o = odds(book("fanduel", -110, 120), book("pinnacle", -110, 105))
    rows = bm.build_rows(o, NOW)
    away = rows[0]["sides"][1]
    assert away["light"] == "green" and away["evPct"] >= 2
    assert rows[0]["sides"][0]["light"] == "red"
    assert rows[0]["shadedSide"] == "Home"


def test_spread_needs_same_number():
    o = odds(book("fanduel", -110, -110, home_pt=-3.5), book("pinnacle", -105, -105, home_pt=-3.0))
    rows = bm.build_rows(o, NOW)
    assert rows[0]["sides"][0]["light"] == "none"   # only one other book, different number


def test_huge_edge_is_checked_not_bet():
    o = odds(book("fanduel", -110, 300), book("pinnacle", 100, -120))
    rows = bm.build_rows(o, NOW)
    assert rows[0]["sides"][1]["light"] == "check"


def test_stale_sharp_line_is_yellow():
    o = odds(book("fanduel", -110, 120), book("pinnacle", -110, 105, upd="2026-10-11T08:00:00Z"))
    assert bm.build_rows(o, NOW)[0]["sides"][1]["light"] == "yellow"


def test_started_games_skipped():
    o = odds(book("fanduel", -110, 120), book("pinnacle", -110, 105), start="2026-10-11T11:00:00Z")
    assert bm.build_rows(o, NOW) == []


def test_log_and_closing_line_value():
    o1 = odds(book("fanduel", -110, 120), book("pinnacle", -110, 105))
    log = bm.update_log([], bm.build_rows(o1, NOW), o1, NOW, "t1")
    assert len(log) == 1 and log[0]["team"] == "Away" and log[0]["price"] == 120
    # sharp money comes in on Away before kickoff: close moves to Away +100
    o2 = odds(book("fanduel", -120, 100, upd="2026-10-11T15:50:00Z"),
              book("pinnacle", -105, -105, upd="2026-10-11T15:50:00Z"))
    log = bm.update_log(log, bm.build_rows(o2, datetime(2026, 10, 11, 15, 55, tzinfo=timezone.utc)),
                        o2, datetime(2026, 10, 11, 15, 55, tzinfo=timezone.utc), "t2")
    assert log[0]["closeFairPct"] == 50.0
    log = bm.update_log(log, [], o2, LATER, "t3")
    e = log[0]
    assert e["closed"] and e["clvPct"] == 10.0     # 2.20 x 50% - 1
    sb = bm.scoreboard(log)
    assert sb["closed"] == 1 and sb["beatClose"] == 1 and sb["avgClvPct"] == 10.0
