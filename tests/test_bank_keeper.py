from datetime import datetime, timezone

import bank_keeper as bk


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_tuesday_afternoon_shift_hits_every_daily_check_on_time():
    # Tue Oct 13, 10:30am-4:30pm ET (daylight time = UTC-4)
    todo = bk.planned_times(utc(2026, 10, 13, 14, 30), utc(2026, 10, 13, 20, 30))
    assert [t.strftime("%H:%M") for t, _ in todo] == ["15:05", "17:35", "19:35"]   # 11:05, 1:35, 3:35 ET
    assert [c for _, c in todo] == ["5 15 * * *", "35 17 * * *", "35 19 * * *"]


def test_saturday_and_sunday_extras_only_on_their_day():
    sat = [t.strftime("%H:%M") for t, _ in bk.planned_times(utc(2026, 10, 17, 13, 0), utc(2026, 10, 17, 19, 0))]
    assert sat == ["13:30", "15:05", "17:35", "18:20", "18:45"]
    sun = [t.strftime("%H:%M") for t, _ in bk.planned_times(utc(2026, 10, 18, 15, 0), utc(2026, 10, 18, 19, 0))]
    assert sun == ["15:05", "15:45", "16:30", "17:35", "18:52"]


def test_winter_uses_standard_time_copies():
    todo = bk.planned_times(utc(2026, 12, 15, 15, 0), utc(2026, 12, 15, 17, 0))
    assert [c for _, c in todo] == ["5 16 * * *"]                       # 11:05am ET = 16:05 UTC in December


def test_shifts_back_to_back_never_miss_or_repeat():
    a_start = utc(2026, 10, 13, 12, 0)
    a = bk.planned_times(a_start - bk.timedelta(minutes=3), a_start + bk.timedelta(minutes=327))
    b_start = a_start + bk.timedelta(minutes=331)                     # next shift starts a minute late
    b = bk.planned_times(b_start - bk.timedelta(minutes=3), b_start + bk.timedelta(minutes=327))
    whole = bk.planned_times(a_start - bk.timedelta(minutes=3), b_start + bk.timedelta(minutes=327))
    assert a + b == whole


def test_githubs_late_copy_buys_nothing_after_the_clock_ran(tmp_path, monkeypatch):
    import json
    import bank_refresh as br
    calls = []
    monkeypatch.setattr(br.requests, "get", lambda *a, **k: calls.append(a) or None)
    path = tmp_path / "bank_odds.json"
    path.write_text(json.dumps({"sports": {"basketball_nba": {"fetched_at": "2026-10-13T15:05:30+00:00"}}}))
    monkeypatch.setattr(br, "BANK_ODDS_PATH", str(path))
    monkeypatch.setenv("ODDS_API_KEY", "k")
    monkeypatch.setenv("SCHEDULE", "5 15 * * *")
    monkeypatch.setenv("SOURCE", "schedule")
    notes = br.main(utc(2026, 10, 13, 15, 40))                         # GitHub's copy, 35 min late
    assert calls == [] and "Nothing bought" in notes[0]
