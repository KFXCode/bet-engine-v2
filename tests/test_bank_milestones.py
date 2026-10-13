from datetime import datetime, timezone

import bank_mode as bm

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


def ladder_event(sport_key, key, player, point, fd_price, other_prices, main_line):
    """FanDuel + other books on one milestone, plus FanDuel's normal over/under line for the player."""
    base = key.replace("_alternate", "")

    def b(k, price, with_main=False):
        markets = [{"key": key, "outcomes": [
            {"name": "Over", "description": player, "price": price, "point": point}]}]
        if with_main:
            markets.append({"key": base, "outcomes": [
                {"name": "Over", "description": player, "price": -110, "point": main_line},
                {"name": "Under", "description": player, "price": -110, "point": main_line}]})
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": markets}
    books = [b("fanduel", fd_price, with_main=True)] + [b(k, p) for k, p in other_prices.items()]
    return {"sports": {sport_key: [{"id": "e1", "commence_time": "2026-10-13T00:20:00Z",
                                    "home_team": "Home", "away_team": "Away", "bookmakers": books}]}}


OTHERS = {"draftkings": 600, "betmgm": 650, "fanatics": 600, "betrivers": 700}


def test_bijan_150_rushing_yards_outlier_is_green():
    # normal line 85.5; 150+ is an upside milestone
    odds = ladder_event("americanfootball_nfl", "player_rush_yds_alternate", "Bijan Robinson", 149.5, 900,
                        OTHERS, main_line=85.5)
    row = bm.build_milestones(odds, NOW)[0]
    s = row["sides"][0]
    assert s["team"] == "Bijan Robinson 150+ rushing yards" and s["mainLine"] == 85.5
    # FanDuel +900 vs best other book +700: pays 9.0 vs 7.0 profit = 29% more
    assert s["light"] == "green" and s["outlierPct"] == 28.6 and s["fairPrice"] == 700
    assert row["fairSource"] == "Best of 4 books" and row["isMilestone"]


def test_penix_150_below_his_line_is_not_a_milestone():
    # normal line 219.5; 150+ passing yards is the safe side at -700, not a milestone
    odds = ladder_event("americanfootball_nfl", "player_pass_yds_alternate", "Michael Penix Jr.", 149.5, -700,
                        {"draftkings": -890, "betmgm": -900, "fanatics": -900, "betrivers": -950},
                        main_line=219.5)
    assert bm.build_milestones(odds, NOW) == []


def test_minus_money_above_the_line_is_skipped():
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 5.5, -130,
                        {"draftkings": -150, "betmgm": -150, "fanatics": -160, "betrivers": -150},
                        main_line=4.5)
    assert bm.build_milestones(odds, NOW) == []


def test_curry_10_threes_best_maryland_book_named():
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 650, OTHERS,
                        main_line=4.5)
    s = bm.build_milestones(odds, NOW)[0]["sides"][0]
    assert s["team"] == "Stephen Curry 10+ threes"
    # BetRivers +700 is the best Maryland price; next best is +650 (FanDuel/BetMGM): 7.7% more -> yellow
    assert s["book"] == "betrivers" and s["bookName"] == "BetRivers" and s["light"] == "yellow"


def test_curry_in_line_everywhere_is_red():
    flat = {"draftkings": 650, "betmgm": 650, "fanatics": 640, "betrivers": 650}
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 650, flat,
                        main_line=4.5)
    s = bm.build_milestones(odds, NOW)[0]["sides"][0]
    assert s["light"] == "red" and s["book"] == "fanduel"      # tie goes to FanDuel


def test_puka_11_catches_green_at_betrivers():
    # Oct 12 real prices: BetRivers +1200, FanDuel +800, Fanatics +800, DK +750, BetOnline +700, ESPN +700
    others = {"betrivers": 1200, "fanatics": 800, "draftkings": 750, "betonlineag": 700, "espnbet": 700}
    odds = ladder_event("americanfootball_nfl", "player_receptions_alternate", "Puka Nacua", 10.5, 800,
                        others, main_line=7.5)
    s = bm.build_milestones(odds, NOW)[0]["sides"][0]
    assert s["team"] == "Puka Nacua 11+ receptions"
    assert s["book"] == "betrivers" and s["price"] == 1200 and s["light"] == "green" and s["outlierPct"] == 50.0


def test_slightly_generous_is_yellow_and_too_few_books_is_none():
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 750, OTHERS,
                        main_line=4.5)
    assert bm.build_milestones(odds, NOW)[0]["sides"][0]["light"] == "yellow"   # 7.5 vs 7.0 = 7%
    odds = ladder_event("basketball_nba", "player_threes_alternate", "Stephen Curry", 9.5, 1500,
                        {"draftkings": 600, "betmgm": 650}, main_line=4.5)
    assert bm.build_milestones(odds, NOW)[0]["sides"][0]["light"] == "none"


def test_milestones_kept_out_of_main_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "OUT_PATH", str(tmp_path / "out.json"))
    monkeypatch.setattr(bm, "LOG_PATH", str(tmp_path / "log.json"))
    odds = ladder_event("americanfootball_nfl", "player_rush_yds_alternate", "Bijan Robinson", 149.5, 900,
                        OTHERS, main_line=85.5)
    out = bm.main(NOW, odds=odds)
    assert out["milestoneCounts"]["green"] == 1
    assert all(not e.get("player") or e["market"] != "Milestone rushing yards" for e in out["log"])


def test_anytime_td_compared_book_to_book():
    def b(k, price):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": "player_anytime_td", "outcomes": [
            {"name": "Yes", "description": "Josh Allen", "price": price}]}]}
    odds = {"sports": {"americanfootball_nfl": [{"id": "e1", "commence_time": "2026-10-13T00:20:00Z",
            "home_team": "Home", "away_team": "Away",
            "bookmakers": [b("fanduel", -105), b("draftkings", -140), b("betmgm", -135), b("fanatics", -145),
                           b("betrivers", -130)]}]}}
    row = bm.build_milestones(odds, NOW)[0]
    s = row["sides"][0]
    assert row["market"] == "Anytime TD" and s["team"] == "Josh Allen anytime TD"
    # FanDuel -105 pays 0.952 profit vs best other -130 (0.769): 24% more -> green
    assert s["light"] == "green" and s["fairPrice"] == -130
