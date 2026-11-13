import grade
import sharpen


def game(sport, spread, ml_home, ml_away, margin, spr_home=-110, spr_away=-110, sd=13.0):
    return {"sport": sport, "home": "H", "away": "A", "time": "2026-11-15T18:00:00Z", "sdMargin": sd,
            "projMargin": margin, "homeRtg": 0, "awayRtg": 0, "hfa": 0, "mlHome": ml_home, "mlAway": ml_away,
            "spread": spread, "sprHome": spr_home, "sprAway": spr_away}


def test_small_college_favorite_and_big_dog_blocked_small_dog_kept():
    g = game("NCAAF", -4.5, -190, 160, 6.0)            # home -4.5, away +4.5
    b = sharpen.block_list(g)
    assert "Spread:home" in b and "Spread:away" not in b
    assert "Moneyline:home" in b                        # -190 favorite
    g = game("NCAAF", -10.5, -400, 310, 9.0)           # away +10.5 big dog
    assert "Spread:away" in sharpen.block_list(g)


def test_college_big_favorite_needs_8_to_12_edge():
    # home -10.5 at -110 (52.4% implied). margin 14 / sd 13 -> covers ~61% = ~8.3 pts edge
    assert "Spread:home" not in sharpen.block_list(game("NCAAF", -10.5, -500, 380, 14.0))
    # margin 11 -> covers ~51% = no edge -> blocked
    assert "Spread:home" in sharpen.block_list(game("NCAAF", -10.5, -500, 380, 11.0))


def test_nfl_and_nba_spread_favorites_and_nba_dog_moneylines_blocked():
    b = sharpen.block_list(game("NFL", -3.5, -180, 150, 3.0))
    assert "Spread:home" in b and "Spread:away" not in b and "Moneyline:away" in b   # +150 NFL dog
    b = sharpen.block_list(game("NBA", 6.5, 210, -260, -6.0))
    assert "Moneyline:home" in b and "Spread:away" in b and "Spread:home" not in b


def test_props_overs_dropped_unders_kept():
    props = [{"sport": "NBA", "marketLabel": "Points", "side": "Over"},
             {"sport": "NBA", "marketLabel": "Points", "side": "Under"},
             {"sport": "NFL", "marketLabel": "Passing yards", "side": "Under"},
             {"sport": "NFL", "marketLabel": "Receptions", "side": "Over"}]
    kept = sharpen.sharpen([], props)
    assert [(p["marketLabel"], p["side"]) for p in kept] == [("Points", "Under"), ("Receptions", "Over")]


def test_grader_skips_blocked_sides():
    g = game("NFL", -3.5, -180, 150, 3.0)
    g["block"] = sharpen.block_list(g)
    cands = grade.candidates(g)
    assert all(f'{c["market"]}:{c["selection"]}' not in g["block"] for c in cands)
    assert any(c["market"] == "Spread" and c["selection"] == "away" for c in cands)
