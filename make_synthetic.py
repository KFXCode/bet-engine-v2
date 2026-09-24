"""Synthetic ledger with KNOWN flaws, to prove the agent finds them."""
import numpy as np, pandas as pd
rng = np.random.default_rng(7)
def to_am(p):  # prob (with vig baked in) -> American
    return np.where(p >= 0.5, -100*p/(1-p), 100*(1-p)/p).round()
rows, passes = [], []
for i in range(9000):
    model = "sides" if rng.random() < 0.55 else "props"
    sport = rng.choice(["NFL","NCAAF","NBA","NCAAB"])
    market = rng.choice(["ML","spread"]) if model=="sides" else rng.choice(["pts","reb","ast"])
    true = rng.uniform(0.35, 0.65)
    mkt = np.clip(true + rng.normal(0, 0.03), 0.05, 0.95)          # market at bet time, no-vig
    over = 1.2 if model=="sides" else 2.2                            # props model is overconfident
    mp = np.clip(mkt + over*(true-mkt) + rng.normal(0, 0.04), 0.05, 0.95)
    if market == "reb": mp = np.clip(mp + 0.10, 0.05, 0.95)          # broken input: reb inflated
    close = np.clip(mkt + 0.6*(true-mkt), 0.05, 0.95)                # close moves toward truth
    vig = 0.0227
    edge = (mp - (mkt+vig))*100
    thr = 5 if model=="sides" else 7
    r = dict(bet_id=i, placed_at=pd.Timestamp("2025-09-01")+pd.Timedelta(hours=6*i), sport=sport, model=model,
             market=market, model_prob=round(mp,4), odds=float(to_am(mkt+vig)), odds_other=float(to_am(1-mkt+vig)),
             closing_odds=float(to_am(close+vig)), closing_odds_other=float(to_am(1-close+vig)))
    if 0 < edge and edge < 20:
        if edge >= thr:
            r["result"] = "W" if rng.random() < true else "L"; rows.append(r)
        else:
            passes.append({**r, "pass_reason": "below threshold"})
pd.DataFrame(rows).to_csv("data/ledger.csv", index=False)
pd.DataFrame(passes).to_csv("data/passes.csv", index=False)
print(len(rows), "bets,", len(passes), "passes")
