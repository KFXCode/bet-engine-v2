"""Diagnostics: where is the engine leaking, and is its probability honest?

Every function returns plain DataFrames/dicts so the report can print the arithmetic.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

Z = 1.96


def wilson(wins: float, n: float):
    if n == 0:
        return (np.nan, np.nan)
    p = wins / n
    d = 1 + Z**2 / n
    c = (p + Z**2 / (2 * n)) / d
    h = Z * np.sqrt(p * (1 - p) / n + Z**2 / (4 * n**2)) / d
    return (c - h, c + h)


def mean_ci(x: pd.Series):
    x = x.dropna()
    n = len(x)
    if n < 2:
        return (x.mean() if n else np.nan, np.nan, np.nan)
    m, se = x.mean(), x.std(ddof=1) / np.sqrt(n)
    return (m, m - Z * se, m + Z * se)


def summarize(df: pd.DataFrame) -> dict:
    graded = df[df["result"].isin(["W", "L"])]
    n_wl = len(graded)
    wins = graded["won"].sum()
    lo, hi = wilson(wins, n_wl)
    clv, clv_lo, clv_hi = mean_ci(df["clv_pts"])
    staked = df["profit_u"].notna().sum()
    return {
        "bets": len(df),
        "graded_wl": n_wl,
        "win_rate": wins / n_wl if n_wl else np.nan,
        "win_rate_ci": (lo, hi),
        "breakeven_win_rate": float(np.nanmean(graded["implied_taken"])) if n_wl else np.nan,
        "roi": df["profit_u"].sum() / staked if staked else np.nan,
        "units": df["profit_u"].sum(),
        "clv_pts": clv,
        "clv_ci": (clv_lo, clv_hi),
        "clv_n": int(df["clv_pts"].notna().sum()),
        "beat_close_rate": float((df["clv_pts"].dropna() > 0).mean()) if df["clv_pts"].notna().any() else np.nan,
    }


def segments(df: pd.DataFrame, keys: list[str], min_n: int) -> pd.DataFrame:
    rows = []
    for k, g in df.groupby(keys, dropna=False):
        s = summarize(g)
        k = k if isinstance(k, tuple) else (k,)
        rows.append({**dict(zip(keys, k)), **{c: s[c] for c in ["bets", "win_rate", "breakeven_win_rate", "roi", "clv_pts", "clv_n", "beat_close_rate"]},
                     "clv_lo": s["clv_ci"][0], "clv_hi": s["clv_ci"][1]})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # A segment is a LEAK only if we're confident its CLV is below zero -- not just noisy.
    out["flag"] = np.where((out["clv_n"] >= min_n) & (out["clv_hi"] < 0), "LEAK",
                   np.where((out["clv_n"] >= min_n) & (out["clv_lo"] > 0), "EDGE",
                   np.where(out["clv_n"] < min_n, "thin sample", "")))
    return out.sort_values("clv_pts")


def edge_buckets(df: pd.DataFrame, bins: list[float]) -> pd.DataFrame:
    """Does a bigger claimed edge actually produce bigger CLV? If not, the edge number is inflated."""
    d = df.copy()
    d["edge_bucket"] = pd.cut(d["edge_pts"], bins=bins, right=False)
    return segments(d, ["model", "edge_bucket"], min_n=1).sort_values(["model", "edge_bucket"])


def calibration(df: pd.DataFrame, width: float = 0.05) -> tuple[pd.DataFrame, dict]:
    g = df[df["won"].notna()].copy()
    if g.empty:
        return pd.DataFrame(), {}
    g["bucket"] = (np.floor(g["model_prob"] / width + 1e-9) * width).round(2)
    table = g.groupby(["model", "bucket"]).agg(
        n=("won", "size"), predicted=("model_prob", "mean"), actual=("won", "mean"),
        market=("market_prob_taken", "mean"), closing=("close_prob", "mean")).reset_index()
    table["gap_pts"] = (table["actual"] - table["predicted"]) * 100

    def brier(p):
        m = p.notna()
        return float(((p[m] - g.loc[m, "won"]) ** 2).mean()) if m.any() else np.nan

    scores = {}
    for model, gm in g.groupby("model"):
        sub = gm
        scores[model] = {
            "n": len(sub),
            "brier_model": float(((sub["model_prob"] - sub["won"]) ** 2).mean()),
            "brier_market_at_bet": float(((sub["market_prob_taken"] - sub["won"]) ** 2).mean()),
            "brier_close": float(((sub["close_prob"] - sub["won"]) ** 2).dropna().mean()) if sub["close_prob"].notna().any() else np.nan,
            # Overconfidence check: avg predicted vs avg actual across all bets.
            "avg_pred": float(sub["model_prob"].mean()),
            "avg_actual": float(sub["won"].mean()),
        }
    return table, scores
