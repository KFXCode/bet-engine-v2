import yaml, pandas as pd
from edge_agent.slate import build_card, floor_price
from edge_agent.market import consensus

def bk(key, a, b, sa=None, sb=None):
    m = [{"key": "h2h", "outcomes": [{"name": "Ravens", "price": a}, {"name": "Steelers", "price": b}]}]
    if sa: m.append({"key": "spreads", "outcomes": [{"name": "Ravens", "price": sa, "point": -3.5}, {"name": "Steelers", "price": sb, "point": 3.5}]})
    return {"key": key, "markets": m}

G1 = {"id": "g1", "home_team": "Baltimore Ravens", "away_team": "Pittsburgh Steelers",
      "bookmakers": [bk("pinnacle", 1.62, 2.45, 1.95, 1.87), bk("draftkings", 1.60, 2.50, 1.91, 1.91),
                     bk("fanduel", 1.65, 2.35, 1.93, 1.89), bk("betmgm", 1.61, 2.40, 1.90, 1.92)]}
CFG = yaml.safe_load(open("edge_agent/config.yaml")); CFG["current_params"]["market_blend_w"] = {"sides": 1.0, "props": 1.0}

PICKS = pd.DataFrame([
  dict(sport="NFL", home_team="Ravens", away_team="Steelers", model="sides", market="h2h", selection="Ravens", point=None, player=None, model_prob=0.70),
  dict(sport="NFL", home_team="Ravens", away_team="Steelers", model="sides", market="spreads", selection="Ravens", point=-3.5, player=None, model_prob=0.60),
  dict(sport="NFL", home_team="Ravens", away_team="Steelers", model="sides", market="h2h", selection="Steelers", point=None, player=None, model_prob=0.43),
  dict(sport="NFL", home_team="Ravens", away_team="Steelers", model="sides", market="spreads", selection="Steelers", point=3.5, player=None, model_prob=0.80),
  dict(sport="NFL", home_team="Bears", away_team="Lions", model="sides", market="h2h", selection="Bears", point=None, player=None, model_prob=0.6),
])

def test_card():
    card = build_card(PICKS, {"NFL": [G1]}, CFG)
    by = {(r.market, r.pick.split()[0]): r for r in card.itertuples()}
    ml = by[("h2h", "Ravens")]
    # w = 1.0 in config -> final = model prob; edge vs best price 1.65 (fanduel)
    assert ml.best_book == "fanduel" and ml.best_odds == -154
    assert abs(ml.edge_pts - round((0.70 - 1/1.65) * 100, 2)) < 1e-9          # +9.39
    assert ml.decision == "PASS" and "correlated" in ml.reason                 # EV 0.155 < spread's 0.158
    sp = by[("spreads", "Ravens")]
    assert abs(sp.edge_pts - round((0.60 - 1/1.93) * 100, 2)) < 1e-9          # +8.19 at fanduel
    assert sp.decision == "BET" and abs(sp.ev_per_unit - round(0.60 * 1.93 - 1, 4)) < 1e-9
    assert by[("h2h", "Steelers")].decision == "PASS" and "<" in by[("h2h", "Steelers")].reason
    assert "cap" in by[("spreads", "Steelers")].reason                         # 80% vs ~53% -> > 20 pts
    assert "not found" in by[("h2h", "Bears")].reason
    assert (card["decision"] == "BET").sum() == 1

def test_floor_price():
    # final 0.70, threshold 5 -> need 1/d <= 0.65 -> d >= 1.5385 -> -186
    assert floor_price(0.70, 5) == -186

def test_only_legal_books_recommended():
    card = build_card(PICKS, {"NFL": [G1]}, CFG)
    bet = card[card["decision"] == "BET"].iloc[0]
    assert bet.best_book != "pinnacle"
    # Ravens -3.5: best legal price is 1.93 (fanduel); consensus still includes pinnacle
    assert bet.best_book == "fanduel" and abs(bet.edge_pts - round((0.60 - 1/1.93) * 100, 2)) < 1e-9
