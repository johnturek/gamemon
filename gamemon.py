#!/usr/bin/env python3
"""
Game Monitor CLI - Monitor a single NFL or NCAA MBB game with live play-by-play updates.
Usage: gamemon <sport> [--team TEAM] [--refresh SECONDS]
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import threading
import select
from datetime import datetime
from typing import Optional

try:
    import termios
    import tty
except Exception:  # pragma: no cover
    termios = None
    tty = None

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.live import Live
from rich.text import Text
from rich.layout import Layout

console = Console()

# ESPN API endpoints
ENDPOINTS = {
    "nfl": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        "summary": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={}",
        "sport_type": "football",
    },
    "ncaambb": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
        "summary": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary?event={}",
        "sport_type": "basketball",
    },
}


def fetch_json(url: str) -> dict:
    """Fetch JSON from URL."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        console.print(f"[red]Error fetching data: {e}[/red]")
        return {}


def notify(title: str, message: str):
    """Send macOS notification (no-op on non-macOS)."""
    if sys.platform != "darwin":
        return

    # Escape quotes for AppleScript
    title = title.replace('"', '\\"')
    message = message.replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
        capture_output=True,
    )


def get_games(sport: str) -> list:
    """Fetch today's games for the sport."""
    endpoint = ENDPOINTS[sport]["scoreboard"]
    data = fetch_json(endpoint)

    games = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])

        if len(competitors) < 2:
            continue

        # Get team info (home is usually index 0, away is index 1, but check homeAway field)
        home = away = None
        for comp in competitors:
            if comp.get("homeAway") == "home":
                home = comp
            else:
                away = comp

        if not home or not away:
            home, away = competitors[0], competitors[1]

        status = event.get("status", {})
        state = status.get("type", {}).get("state", "pre")
        detail = status.get("type", {}).get("detail", "")

        games.append({
            "id": event.get("id"),
            "name": event.get("name", ""),
            "home_team": home.get("team", {}).get("abbreviation", "???"),
            "home_name": home.get("team", {}).get("displayName", "Unknown"),
            "home_score": home.get("score", "0"),
            "away_team": away.get("team", {}).get("abbreviation", "???"),
            "away_name": away.get("team", {}).get("displayName", "Unknown"),
            "away_score": away.get("score", "0"),
            "state": state,
            "detail": detail,
            "situation": competition.get("situation", {}),
        })

    return games


def display_games(games: list) -> Optional[dict]:
    """Display games and let user select one."""
    if not games:
        console.print("[yellow]No games found for today.[/yellow]")
        return None

    table = Table(title="Today's Games", box=box.ROUNDED)
    table.add_column("#", style="cyan", width=3)
    table.add_column("Matchup", style="white")
    table.add_column("Score", style="green")
    table.add_column("Status", style="yellow")

    for i, game in enumerate(games, 1):
        matchup = f"{game['away_team']} @ {game['home_team']}"

        if game["state"] == "pre":
            score = "-"
            status = game["detail"]
        elif game["state"] == "post":
            score = f"{game['away_score']} - {game['home_score']}"
            status = "FINAL"
        else:
            score = f"{game['away_score']} - {game['home_score']}"
            status = f"LIVE - {game['detail']}"

        table.add_row(str(i), matchup, score, status)

    console.print(table)
    console.print()

    while True:
        try:
            choice = console.input("[cyan]Select game number (q to quit): [/cyan]")
            if choice.lower() == "q":
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(games):
                return games[idx]
            console.print("[red]Invalid selection.[/red]")
        except ValueError:
            console.print("[red]Enter a number.[/red]")


def get_scores(data: dict, sport: str) -> tuple:
    """Extract current scores from summary data."""
    try:
        # Both NFL and basketball use header.competitions.competitors
        header = data.get("header", {})
        competitions = header.get("competitions", [{}])
        if competitions:
            competitors = competitions[0].get("competitors", [])
            if len(competitors) >= 2:
                # Find home and away scores
                away_score = 0
                home_score = 0
                for comp in competitors:
                    score = int(comp.get("score", 0))
                    if comp.get("homeAway") == "away":
                        away_score = score
                    else:
                        home_score = score
                return (away_score, home_score)
    except (KeyError, ValueError, IndexError):
        pass
    return (0, 0)


def get_plays_nfl(data: dict, last_play_ids: set) -> list:
    """Extract new plays from NFL data."""
    new_plays = []

    # Get plays from current drive
    drives = data.get("drives", {})
    current = drives.get("current", {})
    plays = current.get("plays", [])

    for play in plays:
        play_id = play.get("id")
        if play_id and play_id not in last_play_ids:
            last_play_ids.add(play_id)
            new_plays.append({
                "id": play_id,
                "text": play.get("text", ""),
                "clock": play.get("clock", {}).get("displayValue", ""),
                "period": play.get("period", {}).get("number", 0),
                "down": play.get("start", {}).get("downDistanceText", ""),
                "scoring": play.get("scoringPlay", False),
                "type": play.get("type", {}).get("text", ""),
            })

    return new_plays


def get_plays_basketball(data: dict, last_play_ids: set) -> list:
    """Extract new plays from basketball data."""
    new_plays = []

    plays = data.get("plays", [])

    for play in plays:
        play_id = play.get("id")
        if play_id and play_id not in last_play_ids:
            last_play_ids.add(play_id)
            new_plays.append({
                "id": play_id,
                "text": play.get("text", ""),
                "clock": play.get("clock", {}).get("displayValue", ""),
                "period": play.get("period", {}).get("number", 0),
                "scoring": play.get("scoringPlay", False),
                "team": play.get("team", {}).get("abbreviation", ""),
            })

    return new_plays


def format_period(period: int, sport: str) -> str:
    """Format period number for display."""
    if sport == "nfl":
        if period == 1:
            return "Q1"
        elif period == 2:
            return "Q2"
        elif period == 3:
            return "Q3"
        elif period == 4:
            return "Q4"
        else:
            return f"OT{period - 4}" if period > 4 else f"Q{period}"
    else:
        if period == 1:
            return "1H"
        elif period == 2:
            return "2H"
        else:
            return f"OT{period - 2}" if period > 2 else f"H{period}"


def display_play(play: dict, sport: str) -> str:
    """Return a formatted string for a single play."""
    period = format_period(play.get("period", 0), sport)
    clock = play.get("clock", "")
    text = play.get("text", "")

    time_str = f"[{clock} {period}]" if clock else f"[{period}]"

    if play.get("scoring"):
        if sport == "nfl":
            return f"[bold green]SCORE! {time_str}[/bold green] {text}"
        team = play.get("team", "")
        return f"[green]{time_str} {team}[/green] {text}"

    return f"[dim]{time_str}[/dim] {text}"


def build_leaders_table(data: dict, game: dict) -> Table:
    """Build a small 'player leaders' table from ESPN summary JSON."""
    away_abbr = game.get("away_team", "")
    home_abbr = game.get("home_team", "")

    leaders_by_team = {"away": {}, "home": {}}

    for team_block in data.get("leaders", []) or []:
        team = team_block.get("team", {}) or {}
        abbr = team.get("abbreviation", "")
        side = "away" if abbr == away_abbr else "home" if abbr == home_abbr else None
        if not side:
            continue

        for cat in team_block.get("leaders", []) or []:
            cat_name = cat.get("displayName") or cat.get("name") or ""
            entries = (cat.get("leaders") or [])
            if not entries:
                continue
            top = entries[0]
            athlete = (top.get("athlete") or {}).get("displayName", "")
            disp = top.get("displayValue") or top.get("summary") or ""
            leaders_by_team[side][cat_name] = f"{athlete} ({disp})" if disp else athlete

    all_cats = sorted(set(leaders_by_team["away"]) | set(leaders_by_team["home"]))

    table = Table(box=box.SIMPLE, show_header=True, expand=True)
    table.add_column("Category", style="cyan", no_wrap=True)
    table.add_column(away_abbr or "Away", style="white")
    table.add_column(home_abbr or "Home", style="white")

    if not all_cats:
        table.add_row("-", "-", "-")
        return table

    for cat in all_cats:
        table.add_row(
            cat,
            leaders_by_team["away"].get(cat, "-"),
            leaders_by_team["home"].get(cat, "-"),
        )

    return table


def _start_key_listener(state: dict, stop_event: threading.Event):
    if termios is None or tty is None or not sys.stdin.isatty():
        return None

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def run():
        try:
            while not stop_event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch in ("\t", "t", "T"):
                    state["tab"] = "stats" if state.get("tab") == "main" else "main"
                elif ch in ("q", "Q"):
                    state["quit"] = True
                    stop_event.set()
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _build_layout(tab: str, header_panel: Panel, plays_panel: Panel, leaders_panel: Panel) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(header_panel, name="header", size=3),
        Layout(name="body"),
    )

    if tab == "stats":
        layout["body"].split_row(
            Layout(leaders_panel, name="leaders", ratio=1),
            Layout(plays_panel, name="plays", ratio=2),
        )
    else:
        layout["body"].update(plays_panel)

    return layout


def display_header(game: dict, scores: tuple, situation: dict, sport: str) -> Panel:
    """Build current game status header."""
    away_score, home_score = scores

    # Build situation text
    sit_text = ""
    if sport == "nfl" and situation:
        down = situation.get("downDistanceText", "")
        poss = situation.get("possession", "")
        if down:
            sit_text = f"  {down}"
        if poss:
            sit_text += f" ({poss} ball)"

    header = f"  {game['away_team']} [bold]{away_score}[/bold] - [bold]{home_score}[/bold] {game['home_team']}    {game['detail']}{sit_text}  "

    return Panel(header, box=box.DOUBLE, style="cyan")


def monitor_game(game: dict, sport: str, refresh: int):
    """Main monitoring loop for a game."""
    event_id = game["id"]
    endpoint = ENDPOINTS[sport]["summary"].format(event_id)

    last_play_ids = set()
    last_scores = (0, 0)
    initialized = False
    play_log: list[str] = []

    state = {"tab": "main", "quit": False}
    stop_event = threading.Event()
    key_thread = _start_key_listener(state, stop_event)

    try:
        with Live(screen=True, auto_refresh=False, console=console) as live:
            while True:
                data = fetch_json(endpoint)

                if not data:
                    time.sleep(refresh)
                    continue

                # Check game state
                header = data.get("header", {})
                competitions = header.get("competitions", [{}])
                if competitions:
                    status = competitions[0].get("status", {})
                    game["state"] = status.get("type", {}).get("state", game["state"])
                    game["detail"] = status.get("type", {}).get("detail", game["detail"])

                # Get current scores
                scores = get_scores(data, sport)

                # Get situation for NFL
                situation = {}
                if sport == "nfl":
                    situation = data.get("situation", {})
                    if not situation:
                        drives = data.get("drives", {})
                        current = drives.get("current", {})
                        if current:
                            plays = current.get("plays", [])
                            if plays:
                                last_play = plays[-1]
                                situation = {
                                    "downDistanceText": last_play.get("end", {}).get("downDistanceText", ""),
                                    "possession": last_play.get("end", {}).get("team", {}).get("abbreviation", ""),
                                }

                # Get new plays
                if sport == "nfl":
                    new_plays = get_plays_nfl(data, last_play_ids)
                else:
                    new_plays = get_plays_basketball(data, last_play_ids)

                if not initialized:
                    play_log.append(f"[bold cyan]Monitoring:[/bold cyan] {game['away_name']} @ {game['home_name']}")
                    play_log.append(f"[dim]Refreshing every {refresh} seconds. Press Ctrl+C to quit.[/dim]")
                    play_log.append("")

                    for play in new_plays[-10:]:
                        play_log.append(display_play(play, sport))

                    initialized = True
                    last_scores = scores
                else:
                    for play in new_plays:
                        play_log.append(display_play(play, sport))

                        # Notify on scoring plays
                        if play.get("scoring"):
                            notify(
                                f"{game['away_team']} vs {game['home_team']}",
                                f"SCORE! {scores[0]} - {scores[1]}"
                            )

                    # Check for score changes
                    if scores != last_scores:
                        if scores[0] > last_scores[0]:
                            notify(
                                f"{game['away_team']} Scores!",
                                f"{game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}"
                            )
                        elif scores[1] > last_scores[1]:
                            notify(
                                f"{game['home_team']} Scores!",
                                f"{game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}"
                            )

                    last_scores = scores

                # Trim play log to fit screen
                max_lines = max(10, console.size.height - (6 if state.get("tab") == "stats" else 5))
                if len(play_log) > max_lines:
                    play_log = play_log[-max_lines:]

                header_panel = display_header(game, scores, situation, sport)
                leaders_panel = Panel(build_leaders_table(data, game), title="Player Leaders", box=box.ROUNDED)
                plays_text = Text.from_markup("\n".join(play_log)) if play_log else Text("Waiting for plays...", style="dim")
                plays_panel = Panel(
                    plays_text,
                    title="Play by Play",
                    subtitle="Tab/T: stats  |  Q: quit  |  Ctrl+C: quit",
                    box=box.ROUNDED,
                )

                layout = _build_layout(state.get("tab", "main"), header_panel, plays_panel, leaders_panel)
                live.update(layout, refresh=True)

                if state.get("quit"):
                    break

                # Check if game ended
                if game["state"] == "post":
                    play_log.append("")
                    play_log.append("[bold yellow]FINAL[/bold yellow]")
                    play_log.append(f"[bold]{game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}[/bold]")
                    notify(
                        "Game Over",
                        f"Final: {game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}"
                    )

                    plays_text = Text.from_markup("\n".join(play_log[-max_lines:]))
                    plays_panel = Panel(plays_text, title="Play by Play", box=box.ROUNDED)
                    leaders_panel = Panel(build_leaders_table(data, game), title="Player Leaders", box=box.ROUNDED)

                    layout = _build_layout(state.get("tab", "main"), header_panel, plays_panel, leaders_panel)
                    live.update(layout, refresh=True)
                    break

                time.sleep(refresh)

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped monitoring.[/yellow]")
    finally:
        stop_event.set()
        if key_thread:
            key_thread.join(timeout=0.2)


def main():
    parser = argparse.ArgumentParser(description="Monitor a live sports game")
    parser.add_argument(
        "sport",
        choices=["nfl", "ncaambb"],
        help="Sport to monitor (nfl or ncaambb)",
    )
    parser.add_argument(
        "--team",
        "-t",
        help="Auto-select game with this team abbreviation",
    )
    parser.add_argument(
        "--refresh",
        "-r",
        type=int,
        default=10,
        help="Refresh interval in seconds (default: 10)",
    )

    args = parser.parse_args()

    console.print(f"\n[bold]Game Monitor - {args.sport.upper()}[/bold]\n")

    # Fetch games
    with console.status("Fetching games..."):
        games = get_games(args.sport)

    if not games:
        console.print("[red]No games available.[/red]")
        sys.exit(1)

    # Auto-select by team if specified
    selected_game = None
    if args.team:
        team_upper = args.team.upper()
        for game in games:
            if team_upper in game["home_team"].upper() or team_upper in game["away_team"].upper():
                selected_game = game
                console.print(f"[green]Auto-selected: {game['away_team']} @ {game['home_team']}[/green]")
                break
        if not selected_game:
            console.print(f"[yellow]Team '{args.team}' not found. Showing all games.[/yellow]\n")

    # Manual selection if not auto-selected
    if not selected_game:
        selected_game = display_games(games)

    if not selected_game:
        console.print("[yellow]No game selected.[/yellow]")
        sys.exit(0)

    # Start monitoring
    monitor_game(selected_game, args.sport, args.refresh)


if __name__ == "__main__":
    main()
