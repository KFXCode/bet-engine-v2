"""
tests/test_sharp_price.py
==========================
NBA/NHL picks are judged against the sharp books' fair price (Oct 28, 2026).
Run with:  python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date

import config
from engine.models import Game, MoneylineOdds
from engine.scoring import evaluate_game
from engine import strategy_rules
from data import odds_providers as op


def _event(fd, sharp, sharp_key="pinnacle", fd_seen="2026-10-28T22:00:00Z", sharp_seen="2026-10-28T21:55:00Z",
           home="Boston Celtics", away="Brooklyn Nets"):
    def bm(key, prices, seen):
        return {"key": key, "last_update": seen, "markets": [{"key": "h2h", "outcomes": [
            {"name": home, "price": prices[0]}, {"name": away, "price": prices[1]}]}]}
    books = [bm("fanduel", fd, fd_seen)]
    if sharp:
        books.append(bm(sharp_key, sharp, sharp_seen))
    return {"home_team": home, "away_team": away, "bookmakers": books}


GAME = Game("nba-1", "2026-10-28", "BOS", "BKN", "2026-10-28T23:30:00Z", sport="NBA")


def _eval(fd, sharp, **kw):
    odds = op._parse_odds_event(_event(fd, sharp, **kw), "fanduel", GAME, "now", "NBA")
    return odds, evaluate_game(GAME, odds, None, None, None, None, {}, {}, None, {}, run_date=date(2026, 10, 28))


class TestSharpPrice(unittest.TestCase):
    def test_asks_for_sharp_books_for_free(self):
        self.assertEqual(op.TheOddsApiProvider(api_key="x", sport="NBA")._bookmakers(),
                         "fanduel,draftkings,betmgm,williamhill_us,betrivers,espnbet,fanatics,"
                         "pinnacle,lowvig,betonlineag")
        self.assertIn("pinnacle", op.TheOddsApiProvider(api_key="x", sport="NCAAB")._bookmakers())
        self.assertIn("betrivers", op.TheOddsApiProvider(api_key="x", sport="NFL")._bookmakers())
        self.assertEqual(op.TheOddsApiProvider(api_key="x", sport="MLB")._bookmakers(), "fanduel")

    def test_picks_the_side_fanduel_overpays(self):
        # Pinnacle: Nets +150 / Celtics -170 (Nets ~39%). FanDuel pays Nets +175.
        odds, ev = _eval((-210, 175), (-170, 150))
        self.assertEqual(odds.sharp_source, "Pinnacle")
        self.assertEqual(ev.recommended_side, "away")
        self.assertGreater(ev.sharp_ev, config.SHARP_MIN_EV)
        plays, _ = strategy_rules.select_daily_plays([ev], _FakeDB(), {}, "2026-10-28")
        self.assertEqual(len(plays), 1)

    def test_no_pick_when_fanduel_matches_the_sharp_price(self):
        _, ev = _eval((-180, 150), (-175, 155))
        self.assertIsNone(ev.recommended_side)
        self.assertIn("needs +4%", ev.dropped_reason)

    def test_no_sharp_price_means_no_pick(self):
        _, ev = _eval((-210, 175), None)
        self.assertIsNone(ev.recommended_side)

    def test_old_sharp_price_is_ignored_and_next_book_used(self):
        odds, _ = _eval((-210, 175), (-170, 150), sharp_seen="2026-10-28T19:00:00Z")
        self.assertIsNone(odds.sharp_home_prob)

    def test_too_good_to_be_real_is_skipped(self):
        _, ev = _eval((-400, 400), (-170, 150))
        self.assertIsNone(ev.recommended_side)
        self.assertIn("stale", ev.dropped_reason)


class TestBestMarylandBook(unittest.TestCase):
    def test_pick_uses_the_best_maryland_book(self):
        # Pinnacle Nets +150 (fair ~39%). FanDuel +140, BetRivers +170 -> pick at BetRivers.
        ev_json = _event((-170, 140), (-170, 150))
        ev_json["bookmakers"].append({"key": "betrivers", "last_update": "2026-10-28T22:00:00Z",
            "markets": [{"key": "h2h", "outcomes": [{"name": "Boston Celtics", "price": -200},
                                                    {"name": "Brooklyn Nets", "price": 170}]}]})
        odds = op._parse_odds_event(ev_json, "fanduel", GAME, "now", "NBA")
        self.assertEqual((odds.best_away_ml, odds.best_away_book), (170, "betrivers"))
        ev = evaluate_game(GAME, odds, None, None, None, None, {}, {}, None, {}, run_date=date(2026, 10, 28))
        self.assertEqual(ev.recommended_side, "away")
        self.assertEqual(ev.odds.away_ml, 170)
        self.assertEqual(strategy_rules._book_for(ev, "away"), "BetRivers")

    def test_nfl_falls_back_to_sharp_when_model_has_nothing(self):
        g = Game("nfl-1", "2026-10-28", "BUF", "BAL", "2026-10-28T23:30:00Z", sport="NFL")
        ev_json = _event((-210, 175), (-170, 150), home="Buffalo Bills", away="Baltimore Ravens")
        odds = op._parse_odds_event(ev_json, "fanduel", g, "now", "NFL")
        ev = evaluate_game(g, odds, None, None, None, None, {}, {}, None, {}, run_date=date(2026, 10, 28))
        self.assertEqual(ev.recommended_side, "away")
        self.assertIsNotNone(ev.sharp_ev)

    def test_college_scoreboards_ask_for_every_division_game(self):
        from data import espn_fetch
        self.assertEqual(espn_fetch.DIVISION_GROUPS["basketball/mens-college-basketball"], "50")
        self.assertEqual(espn_fetch.DIVISION_GROUPS["football/college-football"], "80")

    def test_ncaaf_totals_off(self):
        self.assertEqual(config.TOTALS_SPORTS, [])


class _FakeDB:
    def get_recent_team_picks(self, *a, **k):
        return []

    def get_opening_line(self, *a, **k):
        return None

    def __getattr__(self, name):
        return lambda *a, **k: None


if __name__ == "__main__":
    unittest.main()
