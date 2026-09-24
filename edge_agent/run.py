"""Edge Agent: weekly self-review for the Cover1Picks Edge Engine.

  python -m edge_agent.run --config edge_agent/config.yaml

Writes:
  reports/edge_agent/<date>.md         human-readable review with all arithmetic
  reports/edge_agent/proposals.json    machine-readable proposed param changes (empty if none pass)
The GitHub Action turns a non-empty proposals.json into a PR. It never edits model code or merges.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import diagnostics as dx
from . import ledger, tuning


def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def pts(x, d=2):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{d}f}"


def md_table(df: pd.DataFrame, fmt: dict) -> str:
    if df is None or df.empty:
        return "_no data_\n"
    cols = list(fmt)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(fmt[c](r[c]) if callable(fmt[c]) else str(r[c]) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="edge_agent/config.yaml")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    S, P = cfg["samples"], cfg["current_params"]

    df = ledger.load(cfg["ledger_path"], cfg["columns"])
    passes = None
    if cfg.get("passes_path") and Path(cfg["passes_path"]).exists():
        passes = ledger.load(cfg["passes_path"], cfg["columns"], is_pass=True)

    out_dir = Path(cfg.get("output_dir", "reports/edge_agent"))
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    R, proposals = [], []

    R.append(f"# Edge Agent review, {today}\n")
    R.append("Target metric: **CLV** (did we beat the closing price), then calibration. Win rate is shown "
             "for context only. Chasing it directly pushes toward heavy favorites and can lose money.\n")

    # 1. Headline, per model
    R.append("## 1. Headline by model\n")
    for model, g in df.groupby("model"):
        s = dx.summarize(g)
        R.append(f"**{model}**: {s['bets']} bets · CLV {pts(s['clv_pts'])} pts "
                 f"(95% CI {pts(s['clv_ci'][0])} to {pts(s['clv_ci'][1])}, n={s['clv_n']}) · "
                 f"beat close {pct(s['beat_close_rate'])} · win {pct(s['win_rate'])} vs breakeven "
                 f"{pct(s['breakeven_win_rate'])} (95% CI {pct(s['win_rate_ci'][0])} to {pct(s['win_rate_ci'][1])}) · "
                 f"ROI {pct(s['roi'])} · {s['units']:+.2f}u\n")

    # 2. Leaks
    R.append(f"## 2. Leaks by sport × model × market\n\nLEAK = confident negative CLV (upper 95% bound < 0, n ≥ {S['segment_min']}).\n")
    seg = dx.segments(df, ["sport", "model", "market"], S["segment_min"])
    R.append(md_table(seg, {"sport": str, "model": str, "market": str, "bets": str,
                            "clv_pts": pts, "clv_lo": pts, "clv_hi": pts, "beat_close_rate": pct,
                            "win_rate": pct, "roi": pct, "flag": str}))
    for _, r in seg[seg["flag"] == "LEAK"].iterrows():
        proposals.append({"type": "review_segment", "segment": f"{r['sport']}/{r['model']}/{r['market']}",
                          "evidence": f"CLV {r['clv_pts']:+.2f} pts, 95% CI [{r['clv_lo']:+.2f}, {r['clv_hi']:+.2f}], n={r['clv_n']}",
                          "suggestion": "Pause or raise threshold for this segment pending review of its inputs."})

    if "research_verdict" in df.columns and df["research_verdict"].notna().any():
        R.append("\n### Does research actually predict CLV?\n\nResearch gets more say only if these rows separate.\n")
        R.append(md_table(dx.segments(df, ["research_verdict"], S["segment_min"]),
                          {"research_verdict": str, "bets": str, "clv_pts": pts, "clv_lo": pts, "clv_hi": pts, "roi": pct, "flag": str}))

    # 3. Edge monotonicity
    R.append("\n## 3. Does a bigger edge actually mean more CLV?\n\nIf the top buckets don't beat the bottom ones, "
             "the model's edge number is inflated (usually overconfidence).\n")
    R.append(md_table(dx.edge_buckets(df, cfg["edge_bins"]),
                      {"model": str, "edge_bucket": str, "bets": str, "clv_pts": pts, "beat_close_rate": pct, "win_rate": pct, "roi": pct}))

    # 4. Calibration
    R.append("\n## 4. Calibration: does a 60% pick win 60%?\n")
    cal, scores = dx.calibration(df)
    R.append(md_table(cal, {"model": str, "bucket": lambda x: f"{x:.2f}", "n": str, "predicted": pct,
                            "actual": pct, "market": pct, "gap_pts": pts}))
    for m, s in scores.items():
        verdict = "model beats market" if s["brier_model"] < s["brier_market_at_bet"] else "MARKET BEATS MODEL on these bets"
        R.append(f"- **{m}** (n={s['n']}): Brier model {s['brier_model']:.4f} vs market-at-bet "
                 f"{s['brier_market_at_bet']:.4f} vs close {s['brier_close']:.4f} → {verdict}. "
                 f"Avg predicted {pct(s['avg_pred'])} vs actual {pct(s['avg_actual'])}.\n")

    # 5. Tuning proposals (walk-forward)
    R.append(f"\n## 5. Tuning (fit on first {int(S['train_frac']*100)}%, judged on the most recent {100-int(S['train_frac']*100)}%)\n")
    for model, g in df.groupby("model"):
        b = tuning.tune_blend(g, S["train_frac"], S["tuning_min"], P["market_blend_w"].get(model, 1.0),
                              cfg["guardrails"]["blend_floor"], cfg["guardrails"]["blend_max_step"])
        if b["status"] != "ok":
            R.append(f"- **{model}** blend: not enough graded bets ({b['n']}/{b['need']}). No change proposed.\n")
        else:
            R.append(f"- **{model}** blend w: current {b['current_w']:.2f} · unconstrained best {b['best_w']:.2f} · proposed "
                     f"{b['proposed_w']:.2f}. Holdout log-loss {b['test_logloss_current']:.4f} → {b['test_logloss_proposed']:.4f} "
                     f"(market-only {b['test_logloss_market_only']:.4f}).\n")
            if b["hit_floor"]:
                R.append(f"  - ⚠️ On this data the **{model}** model adds little beyond the market price. Blending only hides that. "
                         "Check its inputs (stale logs, injuries, minutes/usage) before trusting its edges.\n")
                proposals.append({"type": "review_model", "segment": model, "evidence": f"best blend {b['best_w']:.2f} < floor {cfg['guardrails']['blend_floor']}",
                                  "suggestion": "Audit model inputs; model not beating market-at-bet out of sample."})
            if b["improves_out_of_sample"]:
                proposals.append({"type": "param", "key": f"market_blend_w.{model}", "from": b["current_w"], "to": b["proposed_w"],
                                  "evidence": f"holdout log-loss {b['test_logloss_current']:.4f} → {b['test_logloss_proposed']:.4f} (n_test={b['n_test']})"})

        pool = pd.concat([g, passes[passes["model"] == model]]) if passes is not None else g
        t = tuning.replay_thresholds(pool.sort_values("placed_at"), model, cfg["threshold_grid"][model],
                                     P["thresholds"][model], S["train_frac"], S["tuning_min"],
                                     cfg["guardrails"]["threshold_max_step"])
        if t["status"] != "ok":
            R.append(f"- **{model}** threshold: not enough CLV data (n={t.get('n')}, need {S['tuning_min']} total and {max(S['tuning_min'] // 4, 20)} in the holdout). No change proposed.\n")
        else:
            R.append(f"- **{model}** threshold: current {t['current']} → best {t['best']:g}. Holdout expected units at close "
                     f"{t['test_ev_current']:+.2f}u → {t['test_ev_best']:+.2f}u · CLV {pts(t['test_clv_current'])} → {pts(t['test_clv_best'])} pts.\n\n")
            R.append(md_table(t["table"], {"threshold": lambda x: f"{x:g}", "train_n": lambda x: f"{x:.0f}", "train_ev_u": lambda x: f"{x:+.2f}u",
                                           "test_n": lambda x: f"{x:.0f}", "test_clv": pts, "test_ev_u": lambda x: f"{x:+.2f}u", "test_roi": pct}))
            if t["improves_out_of_sample"]:
                proposals.append({"type": "param", "key": f"thresholds.{model}", "from": t["current"], "to": float(t["best"]),
                                  "evidence": f"holdout EV at close {t['test_ev_current']:+.2f}u → {t['test_ev_best']:+.2f}u"})

    c = tuning.cap_check(df, P["edge_cap"], cfg["cap_band"], S["segment_min"])
    if c["status"] == "ok":
        R.append(f"\n- Edges within {cfg['cap_band']} pts of the {P['edge_cap']}-pt cap: n={c['n']}, CLV {pts(c['clv'])} "
                 f"(upper bound {pts(c['clv_hi'])}).\n")
        if c["suggest_lower_cap"]:
            proposals.append({"type": "param", "key": "edge_cap", "from": P["edge_cap"], "to": c["suggested_cap"],
                              "evidence": f"near-cap CLV {c['clv']:+.2f}, upper 95% {c['clv_hi']:+.2f}, n={c['n']}"})

    if passes is None:
        R.append("\n> No Passes log supplied, so the agent can only test *raising* thresholds. Add closing odds to "
                 "Passes and set `passes_path` to let it test lowering them too.\n")

    R.append("\n## 6. Proposed changes\n")
    R.append("\n".join((f"- `{p['key']}`: {p['from']} → {p['to']}  ({p['evidence']})" if p["type"] == "param"
                        else f"- **Review `{p['segment']}`**: {p['suggestion']} ({p['evidence']})") for p in proposals) or "_None this week. Nothing cleared the out-of-sample bar._")

    (out_dir / f"{today}.md").write_text("\n".join(R))
    (out_dir / "proposals.json").write_text(json.dumps({"date": today, "proposals": proposals}, indent=2, default=float))
    print(f"wrote {out_dir}/{today}.md · {len(proposals)} proposal(s)")


if __name__ == "__main__":
    main()
