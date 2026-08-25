# Edge Engine

Its own repo, its own link — nothing shared with `mlb-picks` except the odds
API key.

- **Board:** https://kfxcode.github.io/edge-engine/
- **Slate the board reads:** https://kfxcode.github.io/edge-engine/edge_slate.json

## Setup (about five minutes, once)

1. Create a new public repo named **`edge-engine`** under `KFXCode`.
2. Upload everything in this folder to it (`index.html`, `edge_slate.py`,
   `requirements.txt`, `.github/workflows/daily.yml`).
3. **Settings → Secrets and variables → Actions → New repository secret**
   Name `ODDS_API_KEY`, value = the same key `mlb-picks` uses.
4. **Settings → Pages** → Source: *Deploy from a branch*, Branch: `main`, folder `/ (root)`.
5. **Actions → Edge Engine slate → Run workflow.**

The board is live at the link above. It reads the slate on open and every 15
minutes; the workflow refreshes the slate every 30 minutes from 11:00 to 23:59
UTC. Until the first run finishes the board shows its built-in sample slate and
says "Sample slate" in the header, so live and sample are never confused.

## What runs

`edge_slate.py` computes everything itself — no numbers are hand-entered.

| Number | How it's produced |
| --- | --- |
| Team ratings | Iterative adjusted margin over every finished game this season (25 passes, opponent strength cancels, re-centered to 0) |
| Home-field edge | League average of home score − away score |
| Margin spread | RMSE of (rating gap + home edge) vs actual margins |
| Projected total | Own points/game + opponent points allowed/game − league average, both sides |
| Total spread | RMSE of that projection vs actual totals |
| Lines | The Odds API, FanDuel only, American odds |

The board then does the bet math: implied % = 1 ÷ decimal, edge = (our % −
implied %) × 100, EV = p·b − (1 − p), stake = bankroll × Kelly fraction ×
(EV ÷ b). A market is a pick only when edge ≥ 5 points and EV > 0.

Whole-number spreads and totals are dropped — a push is a third outcome this
model doesn't price.

Coverage: NFL, NCAA football, NBA, NCAA basketball — moneyline, spread, total.
No horse racing, soccer, hockey, baseball or props; each needs a different
probability model.

## Notes

Win/loss grading is stored in your browser, per device — the slate file holds
lines and probabilities only.
