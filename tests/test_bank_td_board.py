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
    assert names == ["Only Two Books", "Jahmyr Gibbs", "Amon-Ra St. Brown", "Sam LaPorta"]   # nobody left out
    g = b["picks"][1]
    assert g["fairFrom"] == "Pinnacle" and 71 < g["chancePct"] < 73
    assert g["bestBook"] == "BetRivers" and g["bestPrice"] == -260
    assert b["picks"][3]["value"] and b["picks"][3]["bestBook"] == "BetRivers"   # +250 vs ~+210 fair
    assert [r["player"] for r in b["valuePicks"]] == ["Sam LaPorta"]


def test_one_book_player_left_out_unless_pinnacle_two_way():
    # Backup TE: one Maryland book, nothing to compare -> out.
    # Gibbs: one Maryland book plus Pinnacle Yes/No (a real fair price) -> in.
    odds = _odds({"Backup TE": {"fanduel": 900}, "Jahmyr Gibbs": {"fanduel": -280}})
    names = [r["player"] for r in bm.build_td_board(odds, NOW)["picks"]]
    assert "Backup TE" not in names and "Jahmyr Gibbs" in names


def test_started_games_left_out():
    odds = _odds({"Jahmyr Gibbs": {"fanduel": -280, "draftkings": -300, "betmgm": -290, "betrivers": -260}})
    assert bm.build_td_board(odds, datetime(2026, 11, 8, 19, 0, tzinfo=timezone.utc))["picks"] == []
