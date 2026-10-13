"""Bank Mode's on-time clock.

GitHub's own scheduler runs late (2-7 hours late on Oct 12-13), so the price
checks in bank_refresh.py can't rely on it. This script runs inside one long
GitHub job (.github/workflows/bank-keeper.yml). It sleeps until each planned
Eastern time, starts the Bank Mode refresh right then, and just before the
job's time is up it starts the next keeper so the clock never stops.

Starting a workflow by hand ("workflow_dispatch") runs within seconds, unlike
the scheduler, so every price check happens on time.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import bank_refresh

KEEP_MINUTES = int(os.environ.get("KEEP_MINUTES", "330"))   # GitHub stops a job at 360


def planned_times(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """Every planned refresh between start and end: (UTC time, its schedule line)."""
    out = []
    offset = bank_refresh.eastern_offset(start)
    day = (start - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for cron, (_s, _h, _p, off) in bank_refresh.PLAN.items():
            if off != offset:
                continue
            minute, hour, _dom, _mon, dow = cron.split()
            t = day.replace(hour=int(hour) % 24, minute=int(minute))
            if int(hour) >= 24:
                t += timedelta(days=1)
            # cron weekday: 0 = Sunday ... 6 = Saturday; Python: Monday = 0
            if dow != "*" and (t.weekday() + 1) % 7 != int(dow):
                continue
            if start < t <= end:
                out.append((t, cron))
        day += timedelta(days=1)
    return sorted(set(out))


def dispatch(workflow: str, **inputs) -> None:
    cmd = ["gh", "workflow", "run", workflow, "--ref", os.environ.get("GITHUB_REF_NAME", "main")]
    for k, v in inputs.items():
        cmd += ["-f", f"{k}={v}"]
    for attempt in range(3):
        if subprocess.run(cmd).returncode == 0:
            return
        time.sleep(20)
    print(f"could not start {workflow}", file=sys.stderr)


def main() -> None:
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=KEEP_MINUTES)
    # Each shift covers (its start - 3 min, its end - 3 min], so a check that falls
    # in the few seconds between one shift ending and the next starting isn't lost.
    todo = planned_times(start - timedelta(minutes=3), end - timedelta(minutes=3))
    print("Planned price checks this shift (UTC):", [t.strftime("%a %H:%M") for t, _ in todo])
    for t, cron in todo:
        wait = (t - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        print(f"{datetime.now(timezone.utc):%H:%M:%S} starting price check for {cron}")
        dispatch("bank-mode.yml", plan=cron)
    left = (end - datetime.now(timezone.utc)).total_seconds()
    if left > 0:
        time.sleep(left)
    dispatch("bank-keeper.yml")     # hand off to the next shift


if __name__ == "__main__":
    main()
