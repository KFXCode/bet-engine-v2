# MLB Betting Recommendation Engine

A daily research-and-recommendation tool that implements *your* sharpened
moneyline + HR-prop betting system end to end: pulls the data, grades every
game through your rules, and hands you a clean "bet this, this much, because
this" report. You place the bet -- it does the rest.

**Not financial advice. Sports betting involves real risk of loss. This tool
enforces the rules you specified; it does not guarantee winning picks.**

## What it does every day

1. Grades yesterday's picks (win/loss) automatically.
2. Pulls today's MLB slate, moneylines (FanDuel), public bet splits, advanced
   stats (FIP/barrel%/hard-hit%), standings, injuries, rest, park factors,
   moon phase + zodiac, and date numerology.
3. Scores every game through 8 grading factors into one **edge %** per game.
4. Applies your non-negotiable rules: never below a 5% edge, flat 1-unit
   sizing, a 2nd play only if it's as strong as the first, team
   diversification, line-movement drop rule.
5. Runs the HR prop workflow automatically alongside moneyline.
6. Maybe builds a bonus small parlay, only on a moon/numerology "green light".
7. Prints a terminal report **and** writes a dark, sportsbook-style HTML
   report you can open in any browser.
8. Logs everything to SQLite so P&L, win rate, and diversification history
   build up automatically.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run_daily.py
```

That's it -- with **zero configuration** this runs in mock-odds mode against
the real MLB schedule and real advanced stats (pybaseball), so you can see
the full pipeline and report format today. Open the HTML file it prints the
path to (`data_store/reports/report_<date>.html`) in your browser.

## Making it real (not mock)

Copy `.env.example` to `.env` and fill in:

- **`ODDS_API_KEY`** -- get a free key at https://the-odds-api.com (500
  free credits/month, plenty for one MLB pull a day) and set `ODDS_MODE=api`.
  This is what turns on real FanDuel moneylines/spreads/totals.
- **Public betting % (tickets/handle)** -- there is no free, legal API for
  this anywhere; sportsbettingdime.com, Action Network, etc. are websites for
  humans, not machines. Default mode is `manual`: each run auto-creates
  `manual_inputs/public_betting_<date>.json` listing every game at a neutral
  50/50. Before you trust a day's "public vs. sharp money" factor, open that
  file and fill in the real tickets%/handle% you read off your usual site.
  Takes under a minute for a normal slate. (`PUBLIC_BETTING_MODE=mock` exists
  purely to demo the pipeline without doing this.)

  **Or skip typing it in by hand:** set `PUBLIC_BETTING_MODE=url` and
  `PUBLIC_BETTING_URL=<a splits page>` (e.g.
  `https://www.sportsbettingdime.com/mlb/odds/`) ONCE, and every run fetches
  + parses that page fresh instead -- `data/public_betting_scraper.py` reads
  each team's name off the page and grabs the percentages next to it. Two
  honest caveats: (1) it's text-pattern matching, not a wired-up API, so it's
  reading the page the same fragile way `pybaseball` reads FanGraphs -- if a
  game shows up "missing" in that day's warnings, the page's real layout
  didn't match what the parser expected, and it may need a tweak; (2) sites
  commonly paywall the money%/handle% number specifically (sportsbettingdime
  gates it behind "SBD Plus") while tickets% stays free -- when only
  tickets% is found, that game is flagged `[Bet% only]` in the report rather
  than faking a handle% number. In the cloud scheduler, flip this in
  `.github/workflows/daily.yml`'s env block instead of `.env` (see the
  comment there).
- **Injuries** -- same idea: `manual_inputs/injuries_<date>.json` is
  auto-created empty each day. Add `{player, status, impact}` entries for
  anything that should weigh on the situational factor.
- **Advanced stats** -- `STATS_MODE=api` (the default) uses `pybaseball`,
  which is free and needs no key, but it scrapes public FanGraphs/Baseball
  Savant leaderboards and can be slow on a first run (it's cached in SQLite
  for 12 hours after that). `STATS_MODE=mock` skips all network calls for
  stats if you just want a fast dry run.

## Running it automatically every day

Two ways to automate this -- pick one.

### Cloud scheduler (recommended -- works even if your laptop is off)

`.github/workflows/daily.yml` runs the whole pipeline on GitHub's servers
and publishes straight to your GitHub Pages link, independent of your own
computer. It doesn't run at one fixed time -- it checks every 30 minutes and
actually publishes once, timed to **~1 hour before today's first pitch**
(`config.AUTO_RUN_LEAD_MINUTES`), whether that's a noon getaway-day game or
a 7pm night slate. An off day with no games still gets a single "no games
today" report at the `DAILY_RUN_HOUR` fallback time. A visible "Updated
<date/time>" line on the report itself tells you at a glance whether it's
today's or stale.

One-time setup:
1. Get this whole project into your GitHub repo. Easiest way, no `git`
   install needed: on your repo's GitHub page, click **Add file -> Upload
   files**, then drag your whole project folder in and commit.
   **Do not upload your `.env` file** -- it has your personal token in it,
   and this repo is public. (The cloud run doesn't need it -- see step 2.)
2. That's it. No secrets to add: publishing back to Pages uses GitHub
   Actions' own built-in token, scoped to just this repo, not your personal
   one from `.env`.
3. Check it worked: your repo's **Actions** tab will show runs every 30 min
   (most just say "Nothing new to publish this check" and exit in seconds --
   that's expected). You can also click **Run workflow** there to trigger
   one immediately instead of waiting.

Every run's report + the SQLite history database get committed back to the
repo automatically, so diversification rules and bankroll tracking carry
over from day to day exactly like running it locally would.

### Local scheduler (alternative -- needs your Mac on and awake)

```bash
python scheduler.py
```

Runs `run_daily.py` every day at `DAILY_RUN_HOUR:DAILY_RUN_MINUTE` (`.env`,
default 10:00 America/New_York) and keeps running until you Ctrl+C it. Your
computer has to be on, awake, and not asleep at that moment, and this
Terminal window/process has to stay running -- the cloud option above
doesn't have either limitation. Best run under a process manager (`tmux`,
`systemd`, `launchd`, `pm2`, etc.) so it survives you closing the terminal.

For a single one-off run instead of either automated option, just use
`python run_daily.py` directly (add `--auto` to make it obey the same
"only publish once, ~1 hour before first pitch" gate as the cloud version).

Lineups (and therefore the HR prop pool's accuracy) firm up 3-4 hours before
first pitch, so a single morning run is a reasonable-but-imperfect read --
re-run `python run_daily.py` later in the day if you want a second look
before locking in.

## Mobile access

The HTML report is mobile-first and installs like an app:

- Every run writes both `report_<date>.html` (dated archive) and
  **`latest.html`** (always overwritten) into `data_store/reports/`.
- Open `latest.html` on your phone and use "Add to Home Screen" (iOS
  Safari share menu, or Chrome's install prompt on Android) -- it gets its
  own icon and opens full-screen with no browser chrome, like a real app.

### Getting a real link (GitHub Pages) -- only needed for LOCAL runs

Already done if you set up the cloud scheduler above -- skip this. This is
only for when you run `python run_daily.py` by hand on your own computer and
still want that run to update your live link: `output/publish_github_pages.py`
pushes `latest.html` straight to a GitHub repo via the API every time
`run_daily.py` finishes, so it serves at a stable link:
`https://<your-username>.github.io/<repo-name>/`. One-time setup:

1. Create a GitHub repo (public, so Pages is free) -- name it whatever you
   want, e.g. `mlb-picks`.
2. GitHub -> **Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens -> Generate new token**. Scope it to just that repo,
   permission **Contents: Read and write**. Copy the token.
3. In that repo: **Settings -> Pages -> Source: Deploy from a branch ->
   Branch: main, folder: / (root)**.
4. In `.env`, set:
   ```
   GITHUB_PAGES_ENABLED=true
   GITHUB_TOKEN=<the token from step 2>
   GITHUB_REPO=<your-username>/<repo-name>
   ```
5. Run `python run_daily.py` once. It'll log the live URL -- that's the
   link. Bookmark it / add it to your home screen on your phone. It quietly
   updates in place every day; nothing to redo.

If `GITHUB_PAGES_ENABLED` is left `false` (the default), nothing changes --
the tool just writes the local HTML files like before. A publish failure
(bad token, no internet) is logged and skipped; it never crashes the daily
run.

## Reading the output

- **Terminal**: prints immediately, full reasoning per play, HR props table,
  dropped-plays log, bankroll line.
- **HTML**: `data_store/reports/report_<date>.html` -- same content, dark
  sportsbook styling, meant to be the thing you actually glance at each
  morning. Open it directly in a browser, no server needed.
- **No bet** is a normal, expected output on plenty of days -- the whole
  point of a 5% minimum edge is that most slates won't clear it.

## Retuning the system

Every threshold and weight lives in `config.py` with comments -- nothing
else in the codebase hard-codes a number. In particular:

- `FACTOR_WEIGHTS` -- how much each grading factor can move the model away
  from the market. They start conservative (sum to 0.30) because you have no
  historical calibration yet. After a few weeks of `backtest/grader.py`
  results, look at `data_store/betting_engine.db`'s `recommendations` +
  `bankroll_log` tables and see which factors' `factor_scores_json` actually
  correlated with wins -- then adjust.
- `data/celestial.py` (`MOON_PHASE_BIAS`, `ZODIAC_ELEMENT_BIAS`) and
  `data/numerology.py` (`NUMBER_BIAS`) -- these encode *a* belief system as a
  starting point since you didn't hand us exact rules for "zodiac energy" or
  "numerology of the date." Edit these tables to match your real read. (The
  Moon sign itself -- which sign it's actually reported "in" -- is real
  astronomy computed from a lunar-position formula, cross-checked against
  mooncalendar.astro-seek.com, not something to edit.)
- `MIN_EDGE`, `DIVERSIFICATION_*`, `LINE_MOVE_*`, `HR_PROP_MIN_SCORE`, unit
  size, bankroll -- all in `config.py`.

## Project layout

```
config.py                   Every tunable, in one place
run_daily.py                 Main entrypoint -- run this each day
auto_gate.py                  Decides WHEN the automated run should publish (~1hr before first pitch)
scheduler.py                 Optional: local alternative to the cloud scheduler below
.github/workflows/daily.yml  Cloud scheduler -- runs run_daily.py --auto on GitHub Actions

data/
  db.py                      SQLite schema + all queries
  teams.py                   Canonical team list + name normalization
  schedule_provider.py       Today's slate (MLB Stats API, free/no key)
  standings.py                Win/loss, run diff, games back, streak
  rosters.py                  Active roster batters (for HR props)
  odds_providers.py           Moneyline/spread/total (mock or The Odds API)
  public_betting_provider.py  Tickets/handle % (manual/mock/api)
  stats_provider.py           FIP/barrel%/hard-hit% via pybaseball
  situational.py              Injuries, rest days, park factors
  park_factors.py             Static park factor table
  celestial.py                 Moon phase + zodiac (pure math)
  numerology.py                Date numerology (pure math)

engine/
  models.py                   Shared dataclasses
  grading_factors.py           The 8 scored grading factors
  scoring.py                   Odds de-vig + weighted model -> edge %
  strategy_rules.py            Min edge, sizing, diversification, line moves
  hr_props.py                  HR prop workflow (5 steps)
  parlay.py                    Optional bonus parlay logic

output/
  terminal_report.py           Rich terminal report
  html_report.py + templates/  Dark HTML report
  history_log.py               Persists picks + computes bankroll summary

backtest/
  grader.py                    Grades yesterday's picks, updates P&L

manual_inputs/                 Your daily hand-filled public%/injuries files
tests/                         python -m unittest discover -s tests -t .
data_store/                    SQLite DB + generated HTML reports (gitignored)
```

## Known limitations (read this before trusting it blindly)

- **Public betting % and injuries are manual by default** -- there is no
  free API for either. The tool auto-scaffolds the files; you fill in the
  numbers. Skipping this doesn't break anything, but it does mean the
  "heavily weight public vs. sharp money" rule is running on neutral
  placeholders until you do.
- **pybaseball is a scraper**, not an official API. It's free and reliable
  day-to-day, but if FanGraphs/Baseball Savant change a page layout,
  `data/stats_provider.py` may need a small update. `data/stats_provider.py`
  also sets a normal-browser User-Agent on all outgoing requests -- FanGraphs
  blocks the default Python one with a 403; if you ever see that error
  again, this is the first place to check. Every call is wrapped so a
  broken lookup degrades one factor to neutral rather than
  crashing the run.
- **HR prop batter pool is the active roster**, not a confirmed lineup
  (lineups post ~3-4 hours before first pitch). Good enough for a morning
  scan; re-run later for a tighter read.
- **The edge model has no historical backtest yet** (by your choice, logging
  starts fresh from today). Treat the first few weeks as calibration, review
  `backtest/grader.py` results regularly, and retune `config.FACTOR_WEIGHTS`.
- This tool was generated in an environment that can write Python but can't
  execute it -- it hasn't been run end-to-end. Do a `python run_daily.py`
  dry run yourself before trusting the output, and open an issue-style note
  for yourself on anything that errors so it can be patched.
