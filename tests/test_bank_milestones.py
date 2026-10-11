from datetime import datetime, timezone

import bank_mode as bm

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


def ladder_event(sport_key, key, player, point, fd_price, other_prices):
    def b(k, price):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": key, "outcomes": [
            {"name": "Over", "description": player, "price": price, "point": point}]}]}
    books = [b("fanduel", fd_price)] + [b(k, p) for k, p in other_prices.items()]
    return {"sports": {sport_key: [{"id": "e1", "commence_time": "2026-10-13T00:20:00Z",
                                    "home_team": "Home", "away_team": "Away", "bookmakers": books}]}}


OTHERS = {"draftkings": 600, "betmgm": 650, "fanatics": 600, "betrivers": 700}


def test_bijan_150_rushing_yards_outlier_is_green():
    odds = ladder_event("americanfootball_nfl", "player_rush_yds_alternate", "Bijan Robinson", 149.5, 900, OTHERS)
    row = bm.build_milestones(odds, NOW)[0]
    s = row["sides"][0]
    assert s["team"] == "Bijan Robinson 150+ rushing yards"
    # FanDuel +900 vs best other book +700: pays 9.0 vs 7.0 profit = 29% more
    assert s["light"] == "green" and s["outlierPct"] == 28.6 and s["fairPrice"] == 700
    assert row["fairSource"] == "Best of 4 books" and row["isMilestone"]


def test_curry_10_threes_in_line_is_red():
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 650, OTHERS)
    s = bm.build_milestones(odds, NOW)[0]["sides"][0]
    assert s["team"] == "Stephen Curry 10+ threes"
    assert s["light"] == "red"          # +650 is below the best other book's +700


def test_slightly_generous_is_yellow_and_too_few_books_is_none():
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 750, OTHERS)
    assert bm.build_milestones(odds, NOW)[0]["sides"][0]["light"] == "yellow"   # 7.5 vs 7.0 = 7%
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 1500,
                        {"draftkings": 600, "betmgm": 650})
    assert bm.build_milestones(odds, NOW)[0]["sides"][0]["light"] == "none"


def test_milestones_kept_out_of_main_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "OUT_PATH", str(tmp_path / "out.json"))
    monkeypatch.setattr(bm, "LOG_PATH", str(tmp_path / "log.json"))
    odds = ladder_event("americanfootball_nfl", "player_rush_yds_alternate", "Bijan Robinson", 149.5, 900, OTHERS)
    out = bm.main(NOW, odds=odds)
    assert out["counts"]["green"] == 0 and out["milestoneCounts"]["green"] == 1
    assert out["log"] == []             # milestones are not put in the closing-line log
