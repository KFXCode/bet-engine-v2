from edge_agent.market import devig, consensus, price_pick, decimal_to_american
from edge_agent.ledger import american_to_implied, american_to_decimal
import numpy as np

def test_odds_math():
    assert abs(american_to_implied(-110) - 110/210) < 1e-12        # 52.38% breakeven
    assert abs(american_to_implied(150) - 0.4) < 1e-12
    assert abs(american_to_decimal(-110) - (1 + 100/110)) < 1e-12
    assert decimal_to_american(2.5) == 150 and decimal_to_american(1.5) == -200

def test_devig():
    p = devig([1.909, 1.909])                                        # -110 / -110
    assert abs(p[0] - 0.5) < 1e-9 and abs(sum(p) - 1) < 1e-9

EVENT = {"id": "e1", "bookmakers": [
    {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.80}, {"name": "B", "price": 2.10}]}]},
    {"key": "dk", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.74}, {"name": "B", "price": 2.15}]}]},
    {"key": "fd", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.83}, {"name": "B", "price": 2.00}]},
                             {"key": "spreads", "outcomes": [{"name": "A", "price": 1.91, "point": -3.5}, {"name": "B", "price": 1.91, "point": 3.5}]}]}]}

def test_consensus_weights_and_best_price():
    c = consensus(EVENT, {"pinnacle": 3.0})[("h2h", "", None, "A")]
    pin = (1/1.80) / (1/1.80 + 1/2.10); dk = (1/1.74)/(1/1.74+1/2.15); fd = (1/1.83)/(1/1.83+1/2.00)
    assert abs(c["prob"] - (3*pin + dk + fd) / 5) < 1e-12
    assert c["best_book"] == "fd" and c["best_price"] == 1.83 and c["n_books"] == 3

def test_spread_pairs_by_abs_point():
    c = consensus(EVENT, {})
    assert abs(c[("spreads", "", -3.5, "A")]["prob"] - 0.5) < 1e-9

def test_price_pick():
    r = price_pick(EVENT, "h2h", "A", model_prob=0.60, w=0.5, weights={"pinnacle": 3.0})
    cons = consensus(EVENT, {"pinnacle": 3.0})[("h2h", "", None, "A")]["prob"]
    final = 0.5 * 0.60 + 0.5 * cons
    assert abs(r["final_prob"] - round(final, 4)) < 1e-9
    assert abs(r["edge_pts"] - round((final - 1/1.83) * 100, 2)) < 1e-9
    assert price_pick(EVENT, "spreads", "A", 0.6, 0.5, {}, point=-3.5) is None  # only 1 book < min_books
