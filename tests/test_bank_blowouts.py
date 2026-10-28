from datetime import datetime, timezone

import bank_mode as bm

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


def game(alt_key, alt_by_book, main_market, updated=None):
    """One game: every book posts the normal spread/total plus one alternate line."""
    books = []
    for k, outs in alt_by_book.items():
        books.append({"key": k, "last_update": (updated or {}).get(k, "2026-10-12T21:55:00Z"), "markets": [
            main_market, {"key": alt_key, "outcomes": outs}]})
    return {"sports": {"americanfootball_nfl": [{"id": "g1", "commence_time": "2026-10-13T00:20:00Z",
                                                "home_team": "Buffalo Bills", "away_team": "Atlanta Falcons",
                                                "bookmakers": books}]}}


SPREAD = {"key": "spreads", "outcomes": [{"name": "Buffalo Bills", "price": -110, "point": -3.5},
                                         {"name": "Atlanta Falcons", "price": -110, "point": 3.5}]}
TOTAL = {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 47.5},
                                       {"name": "Under", "price": -110, "point": 47.5}]}


def bills(price):
    return [{"name": "Buffalo Bills", "price": price, "point": -9.5}]


def test_blowout_spread_green_at_the_book_that_pays_most():
    # normal spread Bills -3.5; Bills -9.5 (win by 10+) pays +330 at BetMGM vs +250 best elsewhere
    odds = game("alternate_spreads", {"fanduel": bills(240), "draftkings": bills(250), "betmgm": bills(330),
                                      "fanatics": bills(245), "pinnacle": bills(250)}, SPREAD)
    row = bm.build_milestones(odds, NOW)[0]
    s = row["sides"][0]
    assert row["market"] == "Blowout spread" and row["player"] is None and not row["isProp"]
    assert s["team"] == "Buffalo Bills -9.5 (win by 10+)" and s["mainLine"] == -3.5
    assert s["bookName"] == "BetMGM" and s["light"] == "green" and s["outlierPct"] == 32.0
    assert "Normal spread is -3.5" in s["why"]


def test_spread_inside_the_normal_line_is_not_a_blowout():
    easier = [{"name": "Buffalo Bills", "price": 150, "point": -1.5}]   # easier than -3.5
    odds = game("alternate_spreads", {k: easier for k in ("fanduel", "draftkings", "betmgm", "fanatics", "pinnacle")},
                SPREAD)
    assert bm.build_milestones(odds, NOW) == []


def test_alt_total_over_and_under_both_count_when_past_the_normal_total():
    outs = lambda o, u: [{"name": "Over", "price": o, "point": 54.5}, {"name": "Under", "price": u, "point": 40.5}]
    odds = game("alternate_totals", {"fanduel": outs(300, 280), "draftkings": outs(230, 275),
                                     "betmgm": outs(225, 270), "fanatics": outs(220, 280), "pinnacle": outs(235, 275)},
                TOTAL)
    rows = {r["sides"][0]["team"]: r["sides"][0] for r in bm.build_milestones(odds, NOW)}
    assert rows["Over 54.5 total points"]["light"] == "green"          # FanDuel +300 vs +235
    assert rows["Under 40.5 total points"]["light"] == "red"           # everyone near +280


def test_old_price_is_check_not_green():
    odds = game("alternate_spreads", {"fanduel": bills(240), "draftkings": bills(250), "betmgm": bills(330),
                                      "fanatics": bills(245), "pinnacle": bills(250)}, SPREAD,
                updated={"betmgm": "2026-10-12T19:00:00Z"})
    s = bm.build_milestones(odds, NOW)[0]["sides"][0]
    assert s["light"] == "check" and "older" in s["why"]


def test_two_plus_touchdowns():
    def b(k, price):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": "player_tds_over", "outcomes": [
            {"name": "Over", "description": "James Cook", "price": price, "point": 1.5},
            {"name": "Over", "description": "James Cook", "price": -110, "point": 0.5}]}]}
    odds = {"sports": {"americanfootball_nfl": [{"id": "g1", "commence_time": "2026-10-13T00:20:00Z",
            "home_team": "H", "away_team": "A",
            "bookmakers": [b("fanduel", 550), b("draftkings", 420), b("betmgm", 400), b("fanatics", 425),
                           b("betrivers", 410)]}]}}
    rows = bm.build_milestones(odds, NOW)
    assert len(rows) == 1                                  # 0.5 = scores once, already the anytime TD
    s = rows[0]["sides"][0]
    assert rows[0]["market"] == "Milestone touchdowns" and s["team"] == "James Cook 2+ touchdowns"
    assert s["bookName"] == "FanDuel" and s["light"] == "green"     # +550 vs +425: 29% more


def test_college_games_get_blowout_spreads_and_saturday_buys():
    import bank_refresh as br
    for sport in ("americanfootball_ncaaf", "basketball_ncaab", "basketball_nba", "americanfootball_nfl"):
        assert "alternate_spreads" in br.PROP_MARKETS[sport] and "alternate_totals" in br.PROP_MARKETS[sport]
    sat = [p for p in br.LOCAL_PLAN if p[2] == "6"]
    assert [p[5] for p in sat] == [True, True, False]       # 9:30 and 2:20 buy; the 2:45 backup doesn't


def test_book_that_only_posts_a_harder_number_still_counts():
    # FanDuel 100+ rush yds +1900. DK/MGM/Fanatics post 100+; ESPN only posts 105+ (harder) at +1500.
    def b(k, ladder, main=False):
        outs = [{"name": "Over", "description": "Josh Allen", "price": p, "point": q} for q, p in ladder]
        m = [{"key": "player_rush_yds_alternate", "outcomes": outs}]
        if main:
            m.append({"key": "player_rush_yds", "outcomes": [
                {"name": "Over", "description": "Josh Allen", "price": -110, "point": 36.5},
                {"name": "Under", "description": "Josh Allen", "price": -110, "point": 36.5}]})
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": m}
    odds = {"sports": {"americanfootball_nfl": [{"id": "g1", "commence_time": "2026-10-13T00:20:00Z",
            "home_team": "H", "away_team": "A", "bookmakers": [
                b("fanduel", [(99.5, 1900)], main=True), b("draftkings", [(99.5, 1400)]),
                b("betmgm", [(99.5, 1100)]), b("fanatics", [(99.5, 1500)]),
                b("espnbet", [(104.5, 1500), (149.5, 9000)])]}]}}
    s = bm.build_milestones(odds, NOW)[0]["sides"][0]
    assert s["booksCompared"] == 4                     # ESPN's 105+ counts; its 150+ is too far away
    assert s["light"] == "green" and s["fairPrice"] == 1500 and "harder number" in s["why"]


def test_lottery_ticket_milestones_are_skipped():
    def b(k, price):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": "player_tds_over", "outcomes": [
            {"name": "Over", "description": "Stefon Diggs", "price": price, "point": 2.5}]}]}
    odds = {"sports": {"americanfootball_nfl": [{"id": "g1", "commence_time": "2026-10-13T00:20:00Z",
            "home_team": "H", "away_team": "A",
            "bookmakers": [b("caesars", 20000), b("draftkings", 9000), b("betmgm", 8000), b("fanatics", 9000),
                           b("betrivers", 8500)]}]}}
    assert bm.build_milestones(odds, NOW) == []           # +20000 is past the +2000 cap
