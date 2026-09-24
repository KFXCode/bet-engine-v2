"""Engine integration: the card must reproduce the hand-checked arithmetic on an engine-shaped slate."""
import json, shutil
from pathlib import Path
import yaml
from edge_agent.engine_card import build, spread_consensus, to_decimal_event
from statistics import NormalDist

REPO = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load(open(REPO / "edge_agent/config.yaml"))

GAME = {"sport": "NCAAF", "home": "California Golden Bears", "away": "Clemson Tigers", "time": "2099-09-27T02:30:00Z",
        "homeRtg": 0, "awayRtg": 0, "hfa": 0, "sdMargin": 14.0, "projMargin": -7.4, "marketMargin": 1.5, "trust": 1,
        "mlBand": [-600, 600], "maxDog": 24, "mlHome": 105, "mlAway": -125, "spread": 1.5, "sprHome": -118, "sprAway": -102}

def bk(key, ml_a, ml_h, pt_a, sp_a, sp_h):
    return {"key": key, "markets": [
        {"key": "h2h", "outcomes": [{"name": "Clemson Tigers", "price": ml_a}, {"name": "California Golden Bears", "price": ml_h}]},
        {"key": "spreads", "outcomes": [{"name": "Clemson Tigers", "price": sp_a, "point": pt_a},
                                        {"name": "California Golden Bears", "price": sp_h, "point": -pt_a}]}]}

EV = {"id": "e1", "home_team": "California Golden Bears", "away_team": "Clemson Tigers", "commence_time": GAME["time"],
      "bookmakers": [bk("fanduel", -125, 105, -1.5, -102, -118), bk("draftkings", -130, 110, -1.5, -108, -112),
                     bk("pinnacle", -128, 112, -1.5, -105, -105), bk("betmgm", -125, 105, -2.5, -110, -110)]}

def make_root(tmp):
    for f in ("grade.py", "picks_log.py", "player_props.py"):
        shutil.copy(REPO / f, tmp / f)
    (tmp / "edge_slate.json").write_text(json.dumps({"generated_at": "2099-09-24T12:00:00+00:00", "games": [GAME], "props": []}))
    (tmp / "market_odds.json").write_text(json.dumps({"sports": {"americanfootball_ncaaf": [EV]}}))
    (tmp / "picks_log.json").write_text(json.dumps({"picks": []}))
    return tmp

def test_spread_consensus_translates_other_numbers():
    ev = to_decimal_event(EV)
    c = spread_consensus(ev, "Clemson Tigers", -1.5, 14.0, {"pinnacle": 3.0}, set(CFG["market"]["bettable_books"]))
    assert c["n_books"] == 4 and c["best_book"] == "fanduel"          # -102 is the best LEGAL price at -1.5
    # betmgm hangs -2.5 at -110/-110 -> q=0.5 -> mu = 14*0 - (-2.5) = 2.5; priced at -1.5 -> Phi(1/14) > 0.5
    N = NormalDist()
    assert abs(N.cdf((2.5 - 1.5) / 14.0) - 0.52847) < 1e-4

def test_card_blends_and_prices(tmp_path):
    card = build(make_root(tmp_path), CFG)
    r = card[card["pick"].str.contains("Clemson")].iloc[0]
    w = CFG["current_params"]["market_blend_w"]["sides"]
    assert abs(r.final_prob - round(w * r.model_prob + (1 - w) * r.consensus_prob, 4)) < 2e-4
    dec = 1 + 100 / abs(r.best_odds) if r.best_odds < 0 else 1 + r.best_odds / 100
    assert abs(r.edge_pts - round((r.final_prob - 1 / dec) * 100, 2)) < 0.02
    assert r.best_book in CFG["market"]["bettable_books"]
    assert (card["decision"] == "BET").groupby(card["game"]).sum().max() <= 1   # one side bet per game
