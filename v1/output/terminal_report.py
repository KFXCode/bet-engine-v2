"""
output/terminal_report.py
===========================
Rich-formatted terminal daily report. This is the "just tell me what to bet"
view -- run `python run_daily.py` and read this.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def print_daily_report(report):
    console.rule(f"[bold]MLB Betting Engine -- {report.date}[/bold]")
    console.print(f"Slate size: {report.slate_size} game(s)   |   "
                  f"Moon: {report.celestial.get('phase')} in {report.celestial.get('sign')}   |   "
                  f"Numerology: {report.numerology.get('number')}")

    if report.data_warnings:
        console.print(Panel("\n".join(report.data_warnings), title="Data quality warnings", border_style="yellow"))

    if not report.plays:
        console.print(Panel("NO BET today -- nothing across enabled sports cleared a real edge. That's the "
                             "system working as designed.", title="Recommendation", border_style="red"))
    else:
        for i, play in enumerate(report.plays, start=1):
            _print_play(play, i)

    if report.fade_teams:
        _print_fade_teams(report.fade_teams)

    if report.parlay:
        _print_parlay(report.parlay)

    if report.hr_props:
        _print_hr_props(report.hr_props)
    else:
        console.print("[dim]HR props: no candidates cleared the strongest-signals bar today.[/dim]")

    if report.dropped_notes:
        console.print(Panel("\n".join(report.dropped_notes), title="Plays considered & dropped", border_style="dim"))

    _print_bankroll(report.bankroll_summary)


def _print_play(play, index):
    source_tag = "" if play.odds_source == "fanduel" else "  [SIMULATED ODDS -- real line unavailable]"
    title = f"PLAY #{index}: {play.team} ({play.sport}) ML ({play.odds_american:+d}){source_tag}"
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="left")
    body.add_row(f"[bold]Edge:[/bold] {play.edge_pct:.1%}   "
                 f"[bold]Model win%:[/bold] {play.model_prob:.1%}   "
                 f"[bold]Market win%:[/bold] {play.market_prob:.1%}")
    body.add_row(f"[bold]Stake:[/bold] {play.stake_units:g} unit (${play.stake_dollars:.2f})")
    if play.diversification_flag:
        body.add_row(f"[yellow]Diversification: {play.diversification_flag}[/yellow]")
    if play.line_movement_flag:
        body.add_row(f"[yellow]Line movement: {play.line_movement_flag}[/yellow]")
    body.add_row("")
    body.add_row("[bold]Why:[/bold]")
    for r in play.reasoning:
        body.add_row(f"  - {r}")
    console.print(Panel(body, title=title, border_style="green"))


def _print_fade_teams(fade_teams):
    table = Table(title="Avoid Today (do NOT bet these teams' ML)")
    table.add_column("Team")
    table.add_column("Sport")
    table.add_column("Opp.")
    table.add_column("Odds")
    table.add_column("Why")
    for f in fade_teams:
        table.add_row(f.team, f.sport, f.opponent,
                      f"{f.odds_american:+d}" if f.odds_american is not None else "--",
                      f.reasoning[0])
    console.print(table)


def _print_parlay(parlay):
    legs_desc = " + ".join(f"{leg.team} ML" for leg in parlay.legs)
    console.print(Panel(
        f"{legs_desc}\nCombined odds: {parlay.combined_odds_american:+d}   "
        f"Combined win%: {parlay.combined_prob:.1%}\n{parlay.reasoning}",
        title="Optional parlay (bonus, not a substitute)", border_style="magenta",
    ))


def _print_hr_props(hr_props):
    table = Table(title="HR Props")
    table.add_column("Player")
    table.add_column("Team")
    table.add_column("Vs.")
    table.add_column("Score")
    table.add_column("Why")
    for prop in hr_props:
        table.add_row(prop["player_name"], prop["team"], prop["opponent_pitcher"],
                      f"{prop['score']:.0f}", "; ".join(prop["reasoning"]))
    console.print(table)


def _print_bankroll(summary):
    if not summary:
        return
    console.rule("Bankroll")
    console.print(f"Record: {summary.get('wins', 0)}-{summary.get('losses', 0)}  "
                  f"Units: {summary.get('units_net', 0):+.2f}  "
                  f"$: {summary.get('dollars_net', 0):+.2f}  "
                  f"Bankroll: ${summary.get('running_bankroll', 0):.2f}")
