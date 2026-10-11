import json
from datetime import datetime, timezone

import bank_mode as bm
import bank_refresh as br

NOW = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, data, code=200):
        self.status_code, self._d, self.text = code, data, json.dumps(data)
        self.headers = {"x-requests-remaining": "999"}

    def json(self):
        return self._d


def nba_event():
    b = lambda k, h, a: {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [
        {"key": "h2h", "outcomes": [{"name": "Home", "price": h}, {"name": "Away", "price": a}]}]}
    return {"id": "n1", "commence_time": "2026-10-12T23:30:00Z", "home_team": "Home", "away_team": "Away",
            "bookmakers": [b("fanduel", -110, 120), b("pinnacle", -110, 105)]}


def prop_event():
    def b(k, over, under, line=24.5):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Star Player", "price": over, "point": line},
            {"name": "Under", "description": "Star Player", "price": under, "point": line}]}]}
    return {"id": "n1", "commence_time": "2026-10-12T23:30:00Z", "home_team": "Home", "away_team": "Away",
            "bookmakers": [b("fanduel", 110, -140), b("draftkings", -115, -105), b("betmgm", -112, -108),
                           b("fanatics", -115, -105), b("betrivers", -110, -110)]}


def setup(tmp_path, monkeypatch, schedule):
    calls = []

    def fake_get(url, timeout, params):
        calls.append(url)
        if url.endswith("/events"):
            return Resp([{"id": "n1", "home_team": "Home", "away_team": "Away"}] if "basketball_nba" in url else [])
        if "/events/n1/odds" in url:
            return Resp(prop_event())
        return Resp([nba_event()])
    monkeypatch.setattr(br.requests, "get", fake_get)
    monkeypatch.setenv("ODDS_API_KEY", "k")
    monkeypatch.setenv("SCHEDULE", schedule)
    monkeypatch.setattr(br, "BANK_ODDS_PATH", str(tmp_path / "bank_odds.json"))
    monkeypatch.setattr(br, "ENGINE_ODDS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(bm, "OUT_PATH", str(tmp_path / "bank_mode.json"))
    monkeypatch.setattr(bm, "LOG_PATH", str(tmp_path / "bank_log.json"))
    return calls


def test_only_buys_sports_with_games_soon(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch, "0 22 * * *")          # 6pm: not a props run
    notes = br.main(NOW)
    bought = [u for u in calls if u.endswith("/odds")]
    assert bought == [br.ODDS.format(key="basketball_nba")]      # game lines only
    out = json.load(open(tmp_path / "bank_mode.json"))
    assert out["counts"]["green"] == 1
    assert any("skipped (0 credits)" in n for n in notes)


def test_props_bought_on_last_look_and_graded(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch, "45 22 * * *")         # 6:45pm: props run
    br.main(NOW)
    assert any("/events/n1/odds" in u for u in calls)
    out = json.load(open(tmp_path / "bank_mode.json"))
    prop = next(r for r in out["rows"] if r["isProp"])
    over = prop["sides"][0]
    assert over["team"] == "Star Player Over 24.5" and over["point"] is None
    # FanDuel Over +110 vs 4 books near even money -> green on the stricter prop rule
    assert over["light"] == "green" and prop["fairSource"] == "4-book average"
    assert "Points" in out["markets"] and "Moneyline" in out["markets"]


def test_combine_prefers_newer_and_keeps_bank_only_markets():
    bank = {"sports": {"basketball_nba": {
        "fetched_at": "2026-10-11T22:00:00+00:00",
        "events": [{"id": "n1", "bookmakers": [{"key": "fanduel", "markets": [
            {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 220.5},
                                           {"name": "Under", "price": -110, "point": 220.5}]}]}]}],
        "props": {"fetched_at": "2026-10-12T21:00:00+00:00", "events": {"n1": {"id": "n1", "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "player_points", "outcomes": []}]}]}}}}}}
    eng = {"generated_at": "2026-10-12T20:00:00+00:00",
           "sports": {"basketball_nba": [{"id": "n1", "bookmakers": [{"key": "fanduel", "markets": [
               {"key": "h2h", "outcomes": []}]}]}]}}
    c = bm.combine(bank, eng, NOW)
    keys = [m["key"] for m in c["sports"]["basketball_nba"][0]["bookmakers"][0]["markets"]]
    assert keys == ["h2h", "totals", "player_points"]       # engine's newer lines + bank totals + props
    assert c["generated_at"] == "2026-10-12T20:00:00+00:00"


def test_totals_graded():
    def b(k, o, u, line=47.5):
        return {"key": k, "last_update": "2026-10-12T21:55:00Z", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": o, "point": line}, {"name": "Under", "price": u, "point": line}]}]}
    odds = {"sports": {"americanfootball_nfl": [{"id": "g", "commence_time": "2026-10-13T00:20:00Z",
            "home_team": "H", "away_team": "A", "bookmakers": [b("fanduel", 110, -130), b("pinnacle", -102, -108)]}]}}
    row = bm.build_rows(odds, NOW)[0]
    assert row["market"] == "Total" and row["sides"][0]["team"] == "Over 47.5"
    assert row["sides"][0]["light"] == "green"


def test_every_workflow_time_has_a_plan():
    import re
    crons = re.findall(r'cron: "([^"]+)"', open(".github/workflows/bank-mode.yml").read())
    assert crons and set(crons) == set(br.PLAN)
    assert br.PROPS_RUNS - {""} <= set(br.PLAN)
