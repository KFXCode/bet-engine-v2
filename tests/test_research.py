import json, types
from pathlib import Path
import yaml, pandas as pd
from edge_agent import research as rs
from edge_agent.slate import build_card, add_research, render
from tests.test_slate import PICKS, G1

CFG = yaml.safe_load(open("edge_agent/config.yaml")); CFG["current_params"]["market_blend_w"] = {"sides": 1.0, "props": 1.0}; CFG["research"]["model"] = "test-model"

def B(**d):
    return types.SimpleNamespace(**d, model_dump=lambda d=d: d)

def fake_resp(payload, urls):
    return types.SimpleNamespace(content=[
        B(type="server_tool_use", name="web_search"),
        B(type="web_search_tool_result", content=[{"type": "web_search_result", "url": u, "title": "t"} for u in urls]),
        B(type="text", text="Here you go:\n" + json.dumps(payload), citations=None)])

class FakeClient:
    def __init__(self, payload, urls): self.payload, self.urls, self.calls = payload, urls, []
    @property
    def messages(self): return self
    def create(self, **kw):
        self.calls.append(kw); return fake_resp(self.payload, self.urls)

GOOD = {"summary": "Steelers WR1 ruled out; Ravens healthy.", "verdict": "supports", "confidence": "medium",
        "material_news_after_model_run": False, "evidence": [
          {"claim": "Steelers WR1 out (hamstring)", "direction": "supports", "category": "injury", "url": "https://www.steelers.com/news/injury-report/", "after_model_run": False},
          {"claim": "Invented stat", "direction": "supports", "category": "other", "url": "https://made-up.example/x", "after_model_run": False}]}

def test_unsourced_claims_are_dropped():
    res = rs.research_pick({"sport": "NFL", "game": "g", "pick": "p", "market": "spreads", "model": "sides",
                            "final_prob": 0.6, "best_odds": -108}, CFG, FakeClient(GOOD, ["https://www.steelers.com/news/injury-report"]))
    assert len(res["evidence"]) == 1 and res["dropped_unsourced"] == 1

def test_nothing_verifiable_means_neutral():
    bad = {**GOOD, "verdict": "contradicts", "confidence": "high"}
    res = rs.research_pick({"sport": "NFL", "game": "g", "pick": "p", "market": "h2h", "model": "sides",
                            "final_prob": .6, "best_odds": -108}, CFG, FakeClient(bad, []))
    assert res["verdict"] == "neutral" and res["evidence"] == []

def test_research_can_only_downgrade():
    assert rs.apply_to_decision("PASS", {"verdict": "supports", "confidence": "high"})[0] == "PASS"
    assert rs.apply_to_decision("BET", {"verdict": "contradicts", "confidence": "high"})[0] == "HOLD"
    assert rs.apply_to_decision("BET", {"verdict": "contradicts", "confidence": "low"})[0] == "BET"
    assert rs.apply_to_decision("BET", {"material_news_after_model_run": True})[0] == "HOLD"

def test_tool_config_and_prompt():
    c = FakeClient(GOOD, [])
    rs.research_pick({"sport": "NFL", "game": "g", "pick": "p", "market": "h2h", "model": "sides", "final_prob": .6, "best_odds": 120}, CFG, c)
    kw = c.calls[0]
    assert kw["tools"][0]["name"] == "web_search" and kw["tools"][0]["max_uses"] == 5
    assert "IGNORE other people's picks" in kw["system"] and "+120" in kw["messages"][0]["content"]

def test_line_movement(tmp_path):
    d = tmp_path / "americanfootball_nfl"; d.mkdir()
    import copy
    later = copy.deepcopy(G1)
    for b in later["bookmakers"]:
        for m in b["markets"]:
            if m["key"] == "spreads":
                m["outcomes"][0]["price"] -= 0.08; m["outcomes"][1]["price"] += 0.08   # money on Ravens -3.5
    (d / "20260920T120000Z.json").write_text(json.dumps({"events": [G1]}))
    (d / "20260921T120000Z.json").write_text(json.dumps({"events": [later]}))
    m = rs.line_movement(d, "g1", "spreads", "Ravens", -3.5, "", {}, {"pinnacle"})
    assert m["snapshots"] == 2 and m["move_pts"] > 0 and "toward" in rs.describe_move(m)

def test_end_to_end_card_with_hold():
    card = build_card(PICKS, {"NFL": [G1]}, CFG)
    held = add_research(card, CFG, FakeClient({**GOOD, "material_news_after_model_run": True},
                                              ["https://www.steelers.com/news/injury-report/"]))
    assert (held["decision"] == "BET").sum() == 0 and (held["decision"] == "HOLD").sum() == 1
    ok = add_research(card, CFG, FakeClient(GOOD, ["https://www.steelers.com/news/injury-report/"]))
    out = render(ok, "2026-09-24")
    assert "## Why each bet" in out and "[source](https://www.steelers.com/news/injury-report/)" in out
    assert "made-up.example" not in out and "1 claim(s) dropped" in out
