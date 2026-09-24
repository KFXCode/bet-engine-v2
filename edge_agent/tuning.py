"""Walk-forward tuning. Nothing is proposed unless it holds up on data it was NOT fitted on.

Two knobs, both safe to change without touching model code:
  1. market_blend_w  -- final_prob = w * model_prob + (1 - w) * no-vig market prob.
                        Shrinking toward the market is the most reliable calibration fix there is.
  2. edge thresholds -- per model (sides / props). Evaluated on CLV, not win rate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-6


def log_loss(p, y):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def split(df: pd.DataFrame, train_frac: float):
    cut = int(len(df) * train_frac)
    return df.iloc[:cut], df.iloc[cut:]


def tune_blend(df: pd.DataFrame, train_frac: float, min_n: int, current_w: float = 1.0,
               floor: float = 0.3, max_step: float = 0.15) -> dict:
    g = df[df["won"].notna() & df["market_prob_taken"].notna()]
    if len(g) < min_n:
        return {"status": "insufficient_sample", "n": len(g), "need": min_n}
    tr, te = split(g, train_frac)
    grid = np.round(np.arange(0.0, 1.0001, 0.05), 2)

    def ll(frame, w):
        return log_loss(w * frame["model_prob"] + (1 - w) * frame["market_prob_taken"], frame["won"])

    best_w = min(grid, key=lambda w: ll(tr, w))
    res = {
        "status": "ok", "n_train": len(tr), "n_test": len(te), "current_w": current_w, "best_w": float(best_w),
        "test_logloss_current": ll(te, current_w), "test_logloss_best": ll(te, best_w),
        "test_logloss_market_only": ll(te, 0.0),
    }
    # Guardrails: never propose below the floor (w=0 means "no edge ever"), and move at most max_step per review.
    step = float(np.clip(best_w, current_w - max_step, current_w + max_step))
    res["proposed_w"] = round(max(step, floor), 2)
    res["test_logloss_proposed"] = ll(te, res["proposed_w"])
    res["hit_floor"] = bool(best_w < floor)
    res["improves_out_of_sample"] = (res["proposed_w"] != current_w and
                                     res["test_logloss_proposed"] < res["test_logloss_current"] - 1e-4)
    return res


def replay_thresholds(df: pd.DataFrame, model: str, grid: list[float], current: float,
                      train_frac: float, min_n: int, max_step: float = 2) -> dict:
    """Replay what the ledger would look like at each threshold.

    Bets below the current threshold only exist if the Passes log (with closing odds) is supplied,
    so without it we can only evaluate RAISING the threshold.
    """
    d = df[(df["model"] == model) & df["clv_pts"].notna()]
    if len(d) < min_n:
        return {"status": "insufficient_sample", "model": model, "n": len(d), "need": min_n}
    tr, te = split(d, train_frac)
    lowest_seen = d["edge_pts"].min()
    rows = []
    for t in grid:
        if t < lowest_seen - 1e-9 and t < current:
            continue  # no data below what we actually recorded
        a, b = tr[tr["edge_pts"] >= t], te[te["edge_pts"] >= t]
        rows.append({"threshold": t, "train_n": len(a), "train_clv": a["clv_pts"].mean(),
                     "train_ev_u": a["ev_close"].sum(), "test_n": len(b), "test_clv": b["clv_pts"].mean(),
                     "test_ev_u": b["ev_close"].sum(),
                     "test_roi": b["profit_u"].sum() / max(b["profit_u"].notna().sum(), 1)})
    table = pd.DataFrame(rows)
    # Pick the threshold that maximizes TOTAL expected units at the closing price on train
    # (mean alone always favors a tiny, cherry-picked slice), then require it to win on test too.
    elig = table[table["test_n"] >= max(min_n // 4, 20)]
    if elig.empty:
        return {"status": "insufficient_sample", "model": model, "n": int((te["edge_pts"] >= current).sum()), "table": table}
    elig = elig[(elig["threshold"] - current).abs() <= max_step]  # guardrail: small moves only
    if elig.empty:
        return {"status": "insufficient_sample", "model": model, "n": len(te), "table": table}
    best = elig.loc[elig["train_ev_u"].idxmax()]
    cur = table.loc[(table["threshold"] - current).abs().idxmin()]
    return {"status": "ok", "model": model, "current": current, "best": float(best["threshold"]),
            "table": table, "test_clv_current": cur["test_clv"], "test_clv_best": best["test_clv"],
            "test_ev_current": cur["test_ev_u"], "test_ev_best": best["test_ev_u"],
            "improves_out_of_sample": bool(best["threshold"] != current and best["test_ev_u"] > cur["test_ev_u"] + 1.0)}


def cap_check(df: pd.DataFrame, cap: float, band: float, min_n: int) -> dict:
    """Edges just under the credible-edge cap: are they real or model errors?"""
    near = df[(df["edge_pts"] >= cap - band) & (df["edge_pts"] < cap) & df["clv_pts"].notna()]
    if len(near) < min_n:
        return {"status": "insufficient_sample", "n": len(near), "need": min_n}
    m = near["clv_pts"].mean()
    se = near["clv_pts"].std(ddof=1) / np.sqrt(len(near))
    return {"status": "ok", "n": len(near), "clv": m, "clv_hi": m + 1.96 * se,
            "suggest_lower_cap": bool(m + 1.96 * se < 0), "suggested_cap": cap - band}
