# Edge Agent: added to the Edge Engine

The engine is unchanged in how it prices. The board still reads FanDuel exactly as before.
The agent adds a layer on top that decides **which of the engine's picks to actually bet**.

## What changed
| File | Change |
|---|---|
| `edge_slate.py`, `player_props.py` | Request 10 books instead of FanDuel alone. **Same credits**: The Odds API bills every 10 books as one region. FanDuel is still the priced book. A bad book key automatically falls back to FanDuel only. |
| `edge_agent/` (new) | Bet Card, market consensus, research desk, weekly review |
| `.github/workflows/daily.yml` | New step after grading: builds `bet_card.json` / `bet_card.md`. It can't fail the engine run. |
| `.github/workflows/edge-agent-review.yml` (new) | Tuesday review of `picks_log.json` that opens a PR with evidence. It never merges itself. |

## How a pick becomes a BET
1. The engine publishes its picks (same rule as `grade.py`).
2. Consensus = sharp-weighted, no-vig probability across all books. Spreads at different numbers are translated through the engine's own `sdMargin`.
3. Final = 0.3 × engine + 0.7 × consensus (the weekly review tunes this).
4. Edge is measured against the **best price at a Maryland-legal book**, never an offshore one.
5. BET only if edge ≥ 5 (sides) / 7 (props) and under the 20 cap, with enough books, and it's the best bet in that game.
6. The research desk adds sourced evidence and can move a BET to HOLD, never the other way.

## Why 0.3
First 225 graded sides: the engine averaged **65.6% predicted vs 46.7% actual**. FanDuel's no-vig price was the
better predictor (Brier 0.211 vs 0.254). Replaying those picks, the more the probability leaned on the market, the
better it scored, and losses shrank from −31.3u to −4.3u at 0.3. The review raises the weight back toward the model
only when out-of-sample results earn it.

## Turn on
- Secrets: `ODDS_API_KEY` (already set) · `ANTHROPIC_API_KEY` (for research)
- Actions **variable** `RESEARCH_MODEL` = the Claude model ID to use for research
- Confirm `market.bettable_books` in `edge_agent/config.yaml` against Maryland's licensed sportsbook list
- Tests: `pytest tests`
