"""
tests/test_scoring.py
=======================
Run with:  python -m unittest discover -s tests -t .

Covers the math that MUST be right: odds conversion/de-vig, the min-edge
rule, flat sizing, and the "2nd play must be as strong as the 1st" and
diversification rules. Pure math + fakes only -- no network, no pybaseball.
"""

import unittest
from datetime import date

import config
from engine.scoring import american_to_implied_prob, devig_two_way
from engine.models import Game, MoneylineOdds, FactorScore, SideEvaluation
from engine import strategy_rules
from data.numerology import reduce_date
from data.celestial import moon_sign_for, moon_phase_for, MOON_PHASE_BOUNDARIES


class TestOddsMath(unittest.TestCase):
    def test_favorite_implied_prob(self):
        self.assertAlmostEqual(american_to_implied_prob(-150), 150 / 250, places=4)

    def test_underdog_implied_prob(self):
        self.assertAlmostEqual(american_to_implied_prob(130), 100 / 230, places=4)

    def test_devig_sums_to_one(self):
        home, away = devig_two_way(american_to_implied_prob(-150), american_to_implied_prob(130))
        self.assertAlmostEqual(home + away, 1.0, places=6)


class TestNumerologyAndCelestial(unittest.TestCase):
    def test_reduce_date_single_digit_or_master(self):
        n = reduce_date(date(2026, 7, 8))
        self.assertTrue(n in range(1, 10) or n in (11, 22, 33))

    def test_moon_sign_known_date(self):
        # Real Moon position (not sun-sign date range) -- Moon was in Aries
        # on 2026-07-08 per lunar ephemeris, confirmed against
        # mooncalendar.astro-seek.com.
        self.assertEqual(moon_sign_for(date(2026, 7, 8)), "Aries")

    def test_moon_phase_returns_valid_name_and_illumination(self):
        name, illum = moon_phase_for(date(2026, 7, 8))
        self.assertTrue(0.0 <= illum <= 1.0)
        valid_names = {n for _, n in MOON_PHASE_BOUNDARIES}
        self.assertIn(name, valid_names)


class _FakeDB:
    """Minimal stand-in for data.db.Database -- just enough for strategy_rules."""

    def __init__(self, recent_picks=None, opening_lines=None):
        self._recent_picks = recent_picks or []
        self._opening_lines = opening_lines or {}

    def get_recent_team_picks(self, before_date, lookback_days, kind="moneyline"):
        return self._recent_picks

    def get_opening_line(self, game_id):
        return self._opening_lines.get(game_id)


def _make_eval(game_id, home, away, edge, side="home"):
    game = Game(game_id=game_id, date="2026-07-08", home_team=home, away_team=away, game_time_utc=None)
    odds = MoneylineOdds(book="mock", home_ml=-120, away_ml=110, captured_at="now")
    factor_scores = [FactorScore("public_sharp_split", "x", 0.6, 0.07, "strong", "ok")]
    return SideEvaluation(game=game, odds=odds, factor_scores=factor_scores,
                          market_prob_home=0.55, market_prob_away=0.45,
                          model_prob_home=0.62 if side == "home" else 0.4,
                          model_prob_away=0.38 if side == "home" else 0.6,
                          recommended_side=side, edge_pct=edge)


class TestStrategyRules(unittest.TestCase):
    def test_min_edge_enforced(self):
        evals = [_make_eval("1", "NYY", "BOS", edge=0.03)]  # below MIN_EDGE (0.05)
        plays, _ = strategy_rules.select_daily_plays(evals, _FakeDB(), {}, "2026-07-08")
        self.assertEqual(len(plays), 0)

    def test_flat_one_unit_sizing(self):
        evals = [_make_eval("1", "NYY", "BOS", edge=0.08)]
        plays, _ = strategy_rules.select_daily_plays(evals, _FakeDB(), {}, "2026-07-08")
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0].stake_units, config.FLAT_STAKE_UNITS)

    def test_second_play_must_be_as_strong(self):
        evals = [
            _make_eval("1", "NYY", "BOS", edge=0.12),
            _make_eval("2", "LAD", "SF", edge=0.06),  # weaker than primary -> dropped
        ]
        plays, _ = strategy_rules.select_daily_plays(evals, _FakeDB(), {}, "2026-07-08")
        self.assertEqual(len(plays), 1)

    def test_diversification_blocks_repeat_team(self):
        evals = [_make_eval("1", "NYY", "BOS", edge=0.06)]  # clears base MIN_EDGE, not the stricter bar
        db = _FakeDB(recent_picks=[{"date": "2026-07-07", "team": "NYY"}, {"date": "2026-07-06", "team": "NYY"}])
        plays, dropped = strategy_rules.select_daily_plays(evals, db, {}, "2026-07-08")
        self.assertEqual(len(plays), 0)
        self.assertTrue(any("NYY" in d for d in dropped))


if __name__ == "__main__":
    unittest.main()
