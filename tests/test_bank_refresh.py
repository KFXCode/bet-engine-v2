import json
from datetime import datetime, timezone

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


def test_only_buys_sports_with_games_soon(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, timeout, params):
        calls.append(url)
        if url.endswith("/events"):
            return Resp([{"id": "n1"}] if "basketball_nba" in url else [])
        return Resp([nba_event()])
    monkeypatch.setattr(br.requests, "get", fake_get)
    monkeypatch.setenv("ODDS_API_KEY", "k")
    monkeypatch.setenv("SCHEDULE", "0 22 * * *")
    monkeypatch.setattr(br, "BANK_ODDS_PATH", str(tmp_path / "bank_odds.json"))
    monkeypatch.setattr(br, "ENGINE_ODDS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(br, "COMBINED_PATH", str(tmp_path / "combined.json"))
    monkeypatch.setattr(br.bank_mode, "OUT_PATH", str(tmp_path / "bank_mode.json"))
    monkeypatch.setattr(br.bank_mode, "LOG_PATH", str(tmp_path / "bank_log.json"))
    notes = br.main(NOW)
    bought = [u for u in calls if u.endswith("/odds")]
    assert bought == [br.ODDS.format(key="basketball_nba")]       # 2 credits, nothing else
    out = json.load(open(tmp_path / "bank_mode.json"))
    assert out["counts"]["green"] == 1
    assert any("skipped (0 credits)" in n for n in notes)


def test_combine_prefers_newer():
    bank = {"sports": {"basketball_nba": {"fetched_at": "2026-10-12T22:00:00+00:00", "events": ["new"]},
                       "americanfootball_nfl": {"fetched_at": "2026-10-10T10:00:00+00:00", "events": ["oldbank"]}}}
    eng = {"generated_at": "2026-10-11T20:00:00+00:00",
           "sports": {"basketball_nba": ["eng"], "americanfootball_nfl": ["engnfl"]}}
    c = br.combine(bank, eng)
    assert c["sports"]["basketball_nba"] == ["new"]
    assert c["sports"]["americanfootball_nfl"] == ["engnfl"]
    assert c["generated_at"] == "2026-10-12T22:00:00+00:00"


def test_every_workflow_time_has_a_plan():
    import re
    crons = re.findall(r'cron: "([^"]+)"', open(".github/workflows/bank-mode.yml").read())
    assert crons and set(crons) == set(br.PLAN)
