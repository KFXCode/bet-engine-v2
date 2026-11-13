from datetime import datetime, timezone

import bank_mode as bm

NOW = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)


def _odds(players):
    """players: {name: {book: price}} -- plus Pinnacle Yes/No for Gibbs."""
    books = {}
    for name, prices in players.items():
        for bk, p in prices.items():
            books.setdefault(bk, []).append({"name": "Yes", "description": name, "price": p})
    books.setdefault("pinnacle", []).extend([{"name": "Yes", "description": "Jahmyr Gibbs", "price": -300},
                                             {"name": "No", "description": "Jahmyr Gibbs", "price": 240}])
    return {"sports": {"americanfootball_nfl": [{
        "id": "g", "commence_time": "2026-11-08T18:00:00Z", "home_team": "Detroit Lions",
        "away_team": "Minnesota Vikings",
        "bookmakers": [{"key": k, "markets": [{"key": "player_anytime_td", "outcomes": o}]} for k, o in books.items()]}]}}


def test_board_ranks_by_chance_and_names_the_best_book():
    odds = _odds({
        "Jahmyr Gibbs": {"fanduel": -280, "draftkings": -300, "betmgm": -290, "betrivers": -260},
        "Amon-Ra St. Brown": {"fanduel": 100, "draftkings": 105, "betmgm": 110, "betrivers": 100},
        "Sam LaPorta": {"fanduel": 200, "draftkings": 210, "betmgm": 190, "betrivers": 250},
        "Only Two Books": {"fanduel": -500, "draftkings": -500},
    })
    b = bm.build_td_board(odds, NOW)
    names = [r["player"] for r in b["picks"]]
    assert names == ["Jahmyr Gibbs", "Amon-Ra St. Brown", "Sam LaPorta"]   # 2-book player left out
    g = b["picks"][0]
    assert g["fairFrom"] == "Pinnacle" and 71 < g["chancePct"] < 73
    assert g["bestBook"] == "BetRivers" and g["bestPrice"] == -260
    assert b["picks"][2]["value"] and b["picks"][2]["bestBook"] == "BetRivers"   # +250 vs ~+210 fair
    assert 1.4 < b["expectedHits"] < 1.6


def test_started_games_left_out():
    odds = _odds({"Jahmyr Gibbs": {"fanduel": -280, "draftkings": -300, "betmgm": -290, "betrivers": -260}})
    assert bm.build_td_board(odds, datetime(2026, 11, 8, 19, 0, tzinfo=timezone.utc))["picks"] == []
