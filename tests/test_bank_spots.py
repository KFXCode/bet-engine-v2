from datetime import datetime, timezone

import bank_mode as bm

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


def _game(sport_key, home, away, fd, pin, market="h2h", points=None):
    def b(k, prices):
        outs = [{"name": home, "price": prices[0]}, {"name": away, "price": prices[1]}]
        if points:
            outs[0]["point"], outs[1]["point"] = points
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": market, "outcomes": outs}]}
    return {"sports": {sport_key: [{"id": "g", "commence_time": "2026-10-13T00:20:00Z", "home_team": home,
                                    "away_team": away, "bookmakers": [b("fanduel", fd), b("pinnacle", pin)]}]}}


def _side(odds, team):
    row = bm.build_rows(odds, NOW)[0]
    return next(s for s in row["sides"] if s["team"] == team)


def test_nfl_home_underdog_never_green():
    # Browns home dog +180 at FanDuel vs Pinnacle +160/-180: big edge, but skipped
    s = _side(_game("americanfootball_nfl", "Cleveland Browns", "Houston Texans", (180, -260), (160, -180)),
              "Cleveland Browns")
    assert s["evPct"] > 4 and s["light"] == "yellow" and "home underdogs" in s["why"]


def test_nfl_road_favorite_is_hot():
    s = _side(_game("americanfootball_nfl", "Tennessee Titans", "Jacksonville Jaguars", (220, -175), (190, -230)),
              "Jacksonville Jaguars")
    assert s["light"] == "green" and "road favorites" in s["hotSpot"]


def test_college_road_underdog_never_green_but_home_favorite_hot():
    odds = _game("americanfootball_ncaaf", "Alabama Crimson Tide", "East Carolina Pirates", (-175, 260), (-230, 190))
    assert _side(odds, "Alabama Crimson Tide")["hotSpot"]
    odds = _game("americanfootball_ncaaf", "Alabama Crimson Tide", "East Carolina Pirates", (-260, 200), (-210, 175))
    s = _side(odds, "East Carolina Pirates")
    assert s["light"] == "yellow" and "road underdogs" in s["why"]


def test_nba_rebounds_never_green():
    def b(k, o, u):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": "player_rebounds", "outcomes": [
            {"name": "Over", "description": "Jaylen Brown", "price": o, "point": 5.5},
            {"name": "Under", "description": "Jaylen Brown", "price": u, "point": 5.5}]}]}
    odds = {"sports": {"basketball_nba": [{"id": "g", "commence_time": "2026-10-13T00:20:00Z", "home_team": "H",
            "away_team": "A", "bookmakers": [b("fanduel", -140, 115), b("pinnacle", -110, -110)]}]}}
    s = _side(odds, "Jaylen Brown Under 5.5")
    assert s["evPct"] > 4 and s["light"] == "yellow" and "rebounds" in s["why"]
