"""Load the Edge Engine ledger (and optional Passes log) into one normalized frame.

Column names are mapped via config.yaml so this adapts to the engine's real format.
All odds are American. All probabilities are 0-1. All "pts" are percentage points.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = ["bet_id", "placed_at", "sport", "model", "model_prob", "odds_taken", "result"]
OPTIONAL = ["market", "closing_odds", "closing_odds_other", "odds_taken_other", "edge_pts", "pass_reason", "research_verdict"]


def american_to_implied(odds):
    o = np.asarray(odds, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(o < 0, -o / (-o + 100.0), 100.0 / (o + 100.0))


def american_to_decimal(odds):
    o = np.asarray(odds, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(o < 0, 1.0 + 100.0 / -o, 1.0 + o / 100.0)


def no_vig(p_side, p_other):
    """Two-way no-vig probability. Falls back to the raw implied prob when the other side is missing."""
    p_side = np.asarray(p_side, dtype=float)
    p_other = np.asarray(p_other, dtype=float)
    out = p_side / (p_side + p_other)
    return np.where(np.isnan(p_other), p_side, out)


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):  # {"bets": [...]} style
            data = next(v for v in data.values() if isinstance(v, list))
        return pd.DataFrame(data)
    return pd.read_csv(path)


def engine_frame(path: str | Path) -> pd.DataFrame:
    """Cover1Picks Edge Engine picks_log.json -> the agent's column names.
    Closing price = last pre-kickoff entry in each pick's history (exactly what grade.py uses)."""
    rows = []
    for p in json.loads(Path(path).read_text()).get("picks", []):
        h = [x for x in (p.get("history") or []) if x]
        rows.append({"bet_id": p["id"], "placed_at": p.get("first_seen"), "sport": p["sport"],
                     "model": "sides" if p.get("market") in ("Moneyline", "Spread") else "props",
                     "market": f"{p.get('market')}|{p.get('lane', 'value')}", "model_prob": p["p"],
                     "odds_taken": p["price"], "odds_taken_other": p.get("other_price"),
                     "closing_odds": h[-1][1] if h else None, "closing_odds_other": h[-1][2] if h else None,
                     "edge_pts": p.get("edge"), "result": p.get("result"),
                     "research_verdict": p.get("research_verdict")})
    df = pd.DataFrame(rows)
    return df[df["result"].isin(["W", "L", "P"])]


def load(path: str | Path, columns: dict, is_pass: bool = False) -> pd.DataFrame:
    if Path(path).name == "picks_log.json":
        df, columns = engine_frame(path), {k: k for k in REQUIRED + OPTIONAL}
    else:
        df = _read(Path(path))
    rename = {src: dst for dst, src in columns.items() if src and src in df.columns}
    df = df.rename(columns=rename)
    need = [c for c in REQUIRED if not (is_pass and c == "result")]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}. Map them in config.yaml -> columns.")
    for c in OPTIONAL + (["result"] if is_pass else []):
        if c not in df.columns:
            df[c] = np.nan

    df["placed_at"] = pd.to_datetime(df["placed_at"], utc=True, errors="coerce")
    df["model_prob"] = pd.to_numeric(df["model_prob"], errors="coerce")
    if df["model_prob"].max() > 1.0:  # stored as percent
        df["model_prob"] = df["model_prob"] / 100.0
    for c in ["odds_taken", "closing_odds", "closing_odds_other", "odds_taken_other", "edge_pts"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["result"] = df["result"].astype(str).str.strip().str.upper().str[0].map({"W": "W", "L": "L", "P": "P"})
    df["model"] = df["model"].astype(str).str.lower()
    df["is_pass"] = is_pass

    # Price math -- every derived figure is recomputed here so the report shows consistent arithmetic.
    df["implied_taken"] = american_to_implied(df["odds_taken"])
    df["decimal_taken"] = american_to_decimal(df["odds_taken"])
    df["market_prob_taken"] = no_vig(df["implied_taken"], american_to_implied(df["odds_taken_other"]))
    df["edge_calc_pts"] = (df["model_prob"] - df["implied_taken"]) * 100.0
    df["edge_pts"] = df["edge_pts"].fillna(df["edge_calc_pts"])

    close_raw = american_to_implied(df["closing_odds"])
    df["close_prob"] = no_vig(close_raw, american_to_implied(df["closing_odds_other"]))
    # CLV in pts: how much better our price was than the close. Positive = beat the close.
    # Compare like with like: raw-vs-raw implied (vig on both sides roughly cancels).
    df["clv_pts"] = (close_raw - df["implied_taken"]) * 100.0

    # Expected ROI per 1u if the no-vig closing price is the truth. The honest "was this a good bet" number.
    df["ev_close"] = df["close_prob"] * df["decimal_taken"] - 1.0

    df["profit_u"] = np.select(
        [df["result"] == "W", df["result"] == "L", df["result"] == "P"],
        [df["decimal_taken"] - 1.0, -1.0, 0.0],
        default=np.nan,
    )
    df["won"] = np.where(df["result"] == "W", 1.0, np.where(df["result"] == "L", 0.0, np.nan))
    return df.sort_values("placed_at").reset_index(drop=True)
