#!/usr/bin/env python3
"""
Game Monitor CLI - Monitor a single NFL or NCAA MBB game with live play-by-play updates.
Usage: gamemon <sport> [--team TEAM] [--refresh SECONDS]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import threading
import select
from datetime import datetime
from pathlib import Path
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


def load_config() -> dict:
    """Load optional config from ./gamemon_config.json or ~/.config/gamemon/config.json."""
    paths = [
        Path.cwd() / "gamemon_config.json",
        Path(os.path.expanduser("~/.config/gamemon/config.json")),
    ]

    for p in paths:
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue

    return {}


CONFIG = load_config()


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


def _play_tags(play: dict, sport: str) -> set[str]:
    text = (play.get("text") or "").lower()
    ptype = (play.get("type") or "").lower()
    tags: set[str] = set()

    if play.get("scoring"):
        tags.add("score")

    if "penalty" in text or "penal" in ptype or "foul" in text:
        tags.add("penalty")

    if sport == "nfl":
        if "intercept" in text or "intercept" in ptype or "fumble" in text:
            tags.add("turnover")
        if "sack" in text or "sacked" in text:
            tags.add("sack")
        if "punt" in text or "punt" in ptype:
            tags.add("punt")
    else:
        if "turnover" in text or "steal" in text:
            tags.add("turnover")
        if "3" in text and ("pt" in text or "three" in text):
            tags.add("three")

    return tags


def _play_emoji(play: dict, sport: str) -> str:
    """Pick an emoji for a play (best-effort heuristic + optional overrides)."""
    text = (play.get("text") or "").lower()
    ptype = (play.get("type") or "").lower()

    # User overrides: {"touchdown": "🔥"}
    overrides = (CONFIG or {}).get("emoji_overrides", {}) or {}
    if isinstance(overrides, dict):
        for needle, emoji in overrides.items():
            try:
                if needle and emoji and (str(needle).lower() in text or str(needle).lower() in ptype):
                    return str(emoji)
            except Exception:
                continue

    tags = _play_tags(play, sport)

    if "score" in tags:
        if sport == "nfl":
            if "touchdown" in text or "touchdown" in ptype:
                return "🏈"
            if "field goal" in text or "field goal" in ptype:
                return "🎯"
            if "safety" in text or "safety" in ptype:
                return "🛡️"
            if "extra point" in text or "extra point" in ptype:
                return "➕"
            if "two-point" in text or "2-point" in text:
                return "2️⃣"
            return "🟢"

        return "🎯" if "three" in tags else "🏀"

    if "turnover" in tags:
        return "🧤" if sport == "nfl" and ("intercept" in text or "intercept" in ptype) else "💥"

    if "penalty" in tags:
        return "🚩" if sport == "nfl" else "🚨"

    if "punt" in tags:
        return "🦵"

    if "sack" in tags:
        return "🧱"

    if "timeout" in text or "timeout" in ptype:
        return "⏱️"

    if "miss" in text or "incomplete" in text:
        return "❌"

    if "pass" in text or "pass" in ptype:
        return "👐"

    if "left" in text or "right" in text or "up the middle" in text or "rush" in text or "run" in ptype:
        return "🏃"

    return "•"


def display_play(play: dict, sport: str) -> str:
    """Return a formatted string for a single play."""
    period = format_period(play.get("period", 0), sport)
    clock = play.get("clock", "")
    text = play.get("text", "")
    emoji = _play_emoji(play, sport)
    tags = _play_tags(play, sport)

    time_str = f"[{clock} {period}]" if clock else f"[{period}]"

    if "score" in tags:
        if sport == "nfl":
            return f"{emoji} [bold green]SCORE! {time_str}[/bold green] {text}"
        team = play.get("team", "")
        return f"{emoji} [bold green]{time_str} {team}[/bold green] {text}"

    if "turnover" in tags:
        return f"{emoji} [bold yellow]{time_str} TURNOVER[/bold yellow] {text}"

    if "penalty" in tags:
        return f"{emoji} [yellow]{time_str}[/yellow] {text}"

    return f"{emoji} [dim]{time_str}[/dim] {text}"


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

    return Panel(
        header,
        box=box.DOUBLE,
        style="bold bright_white",
        border_style="bright_yellow",
    )


def _theme_css() -> str:
    theme = (CONFIG or {}).get("theme", "dark")
    if theme == "light":
        return """
        Screen { background: white; color: black; }
        #topbar { height: 3; padding: 0 1; }
        #status { color: black; }
        #search { width: 1fr; }
        .hidden { display: none; }
        #pbp_text { padding: 0 1; }
        #leaders { padding: 0 1; }
        """

    return """
    Screen { background: $background; }
    #topbar { height: 3; padding: 0 1; }
    #status { color: ansi_bright_white; }
    #search { width: 1fr; }
    .hidden { display: none; }
    #pbp_text { padding: 0 1; }
    #leaders { padding: 0 1; }
    """


def select_game_textual(games: list) -> Optional[dict]:
    """Textual game picker with incremental search (fallbacks to classic selection)."""
    try:
        from textual.app import App, ComposeResult
        from textual import on
        from textual.containers import Vertical
        from textual.widgets import Footer, Input, Label, ListItem, ListView
    except Exception:
        return display_games(games)

    favorites = set((CONFIG or {}).get("favorites", []) or [])

    def sort_key(g: dict):
        fav = (g.get("home_team") in favorites) or (g.get("away_team") in favorites)
        live = g.get("state") == "in"
        return (0 if fav else 1, 0 if live else 1, g.get("name", ""))

    games_sorted = sorted(games, key=sort_key)

    class GameSelectApp(App[Optional[dict]]):
        CSS = _theme_css()

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("escape", "clear", "Clear"),
        ]

        def __init__(self):
            super().__init__()
            self.all_games = games_sorted
            self.filtered = games_sorted

        def compose(self) -> ComposeResult:
            yield Label("Select a game (type to search, Enter to pick)", id="status")
            yield Input(placeholder="Search team or matchup...", id="search")
            yield ListView(id="list")
            yield Footer()

        def on_mount(self) -> None:
            self._render_list()
            self.query_one("#search", Input).focus()

        def _render_list(self) -> None:
            lv = self.query_one("#list", ListView)
            lv.clear()
            for g in self.filtered:
                matchup = f"{g['away_team']} @ {g['home_team']}"
                status = g.get("detail", "")
                label = f"{matchup}   {status}"
                lv.append(ListItem(Label(label)))

            # Keep a stable selection so Enter on the input can pick the top match.
            try:
                lv.index = 0 if self.filtered else None
            except Exception:
                pass

        @on(Input.Changed)
        def _changed(self, event: Input.Changed) -> None:
            q = (event.value or "").strip().lower()
            if not q:
                self.filtered = self.all_games
            else:
                self.filtered = [
                    g for g in self.all_games
                    if q in (g.get("away_team", "").lower() + " " + g.get("home_team", "").lower() + " " + g.get("name", "").lower())
                ]
            self._render_list()

        @on(Input.Submitted)
        def _submitted(self, event: Input.Submitted) -> None:
            if event.input.id != "search":
                return
            # User expectation: type query then hit Enter to pick.
            if self.filtered:
                self.exit(self.filtered[0])

        @on(ListView.Selected)
        def _selected(self, event: ListView.Selected) -> None:
            idx = event.index
            if 0 <= idx < len(self.filtered):
                self.exit(self.filtered[idx])

        def action_clear(self) -> None:
            self.query_one("#search", Input).value = ""

    return GameSelectApp().run()


def monitor_game(game: dict, sport: str, refresh: int):
    """Main monitoring loop for a game (Textual TUI)."""
    event_id = game["id"]
    endpoint = ENDPOINTS[sport]["summary"].format(event_id)

    try:
        from textual.app import App, ComposeResult
        from textual import on
        from textual.containers import Horizontal, VerticalScroll
        from textual.widgets import Footer, Input, Label, Static

        try:
            from textual.widgets import TabbedContent, TabPane
        except Exception:  # pragma: no cover
            TabbedContent = None
            TabPane = None

    except Exception:
        console.print("[red]Textual is required for gamemon now.[/red]")
        console.print("Install deps: [cyan]python3 -m pip install -r requirements.txt[/cyan]")
        return

    class GameMonitorApp(App):
        CSS = _theme_css()

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("t", "toggle_view", "Toggle view"),
            ("tab", "toggle_view", "Toggle view"),
            ("space", "pause", "Pause"),
            ("f", "follow", "Follow"),
            ("/", "focus_search", "Search"),
            ("escape", "clear_search", "Clear search"),
            ("a", "filter_all", "All"),
            ("s", "filter_scores", "Scores"),
            ("p", "filter_penalties", "Penalties"),
            ("v", "filter_turnovers", "Turnovers"),
        ]

        def __init__(self):
            super().__init__()
            self.last_play_ids: set[str] = set()
            self.last_scores = (0, 0)
            self.initialized = False
            self.static_log: list[str] = []
            self.plays_log: list[dict] = []  # newest-first: {line, raw, tags}
            self._finalized = False
            self._timer = None
            self.paused = False
            self.follow = True
            self.filter_mode = "all"  # all|scores|penalties|turnovers
            self.search_query = ""
            self.net_status = "OK"
            self.last_ok_at: Optional[datetime] = None

        def compose(self) -> ComposeResult:
            yield Static("", id="game_header")
            with Horizontal(id="topbar"):
                yield Label("", id="status")
                yield Input(placeholder="Search (/ to focus, Esc clear)", id="search", classes="hidden")

            if TabbedContent and TabPane:
                with TabbedContent(id="tabs"):
                    with TabPane("Play by Play", id="pbp"):
                        with VerticalScroll(id="pbp_scroll"):
                            yield Static("", id="pbp_text", markup=True)
                    with TabPane("Stats", id="stats"):
                        yield Static("", id="leaders")
            else:
                with VerticalScroll(id="pbp_scroll"):
                    yield Static("", id="pbp_text", markup=True)
                yield Static("", id="leaders")

            yield Footer()

        def on_mount(self) -> None:
            self.refresh_data()
            self._timer = self.set_interval(refresh, self.refresh_data)

        def _set_status(self) -> None:
            updated = self.last_ok_at.strftime("%H:%M:%S") if self.last_ok_at else "-"
            flags = []
            if self.paused:
                flags.append("PAUSED")
            flags.append("FOLLOW" if self.follow else "FREE")
            if self.filter_mode != "all":
                flags.append(self.filter_mode.upper())
            if self.search_query:
                flags.append(f"SEARCH='{self.search_query}'")
            status = f"Net: {self.net_status} | Updated: {updated} | " + " ".join(flags)
            self.query_one("#status", Label).update(status)

        def action_toggle_view(self) -> None:
            if not (TabbedContent and TabPane):
                return
            tabs = self.query_one("#tabs", TabbedContent)
            active = getattr(tabs, "active", None)
            if isinstance(active, str):
                tabs.active = "stats" if active == "pbp" else "pbp"

        def action_pause(self) -> None:
            self.paused = not self.paused
            if self._timer:
                if self.paused:
                    self._timer.pause()
                else:
                    self._timer.resume()
            self._set_status()

        def action_follow(self) -> None:
            self.follow = not self.follow
            self._set_status()

        def action_focus_search(self) -> None:
            search = self.query_one("#search", Input)
            search.remove_class("hidden")
            search.focus()

        def action_clear_search(self) -> None:
            search = self.query_one("#search", Input)
            search.value = ""
            search.add_class("hidden")
            try:
                if self.query("#pbp_scroll"):
                    self.query_one("#pbp_scroll", VerticalScroll).focus()
            except Exception:
                pass

        def action_filter_all(self) -> None:
            self.filter_mode = "all"
            self._render()

        def action_filter_scores(self) -> None:
            self.filter_mode = "scores"
            self._render()

        def action_filter_penalties(self) -> None:
            self.filter_mode = "penalties"
            self._render()

        def action_filter_turnovers(self) -> None:
            self.filter_mode = "turnovers"
            self._render()

        @on(Input.Changed)
        def _search_changed(self, event: Input.Changed) -> None:
            if event.input.id != "search":
                return
            self.search_query = (event.value or "").strip().lower()
            self._render()

        def _append_play(self, play: dict) -> None:
            line = display_play(play, sport)
            raw = (play.get("text") or "").lower()
            tags = _play_tags(play, sport)
            self.plays_log.insert(0, {"line": line, "raw": raw, "tags": tags})

        def _filtered_lines(self) -> list[str]:
            def ok(item: dict) -> bool:
                tags = item.get("tags") or set()
                if self.filter_mode == "scores" and "score" not in tags:
                    return False
                if self.filter_mode == "penalties" and "penalty" not in tags:
                    return False
                if self.filter_mode == "turnovers" and "turnover" not in tags:
                    return False
                if self.search_query and self.search_query not in (item.get("raw") or ""):
                    return False
                return True

            plays = [i["line"] for i in self.plays_log if ok(i)]
            max_lines = int((CONFIG or {}).get("max_plays", 500) or 500)
            return plays[:max_lines]

        def _render(self, header_panel: Optional[Panel] = None, leaders: Optional[Table] = None) -> None:
            if header_panel is not None:
                self.query_one("#game_header", Static).update(header_panel)

            visible_log = self.static_log + self._filtered_lines()
            pbp_renderable = Text.from_markup("\n".join(visible_log)) if visible_log else Text("Waiting for plays...", style="dim")
            self.query_one("#pbp_text", Static).update(pbp_renderable)

            if leaders is not None and self.query("#leaders"):
                self.query_one("#leaders", Static).update(leaders)

            self._set_status()

            if self.follow and self.query("#pbp_scroll"):
                try:
                    self.query_one("#pbp_scroll", VerticalScroll).scroll_home(animate=False)
                except Exception:
                    pass

        def refresh_data(self) -> None:
            if self._finalized:
                return

            data = fetch_json(endpoint)
            if not data:
                self.net_status = "NO DATA"
                self._set_status()
                return

            self.net_status = "OK"
            self.last_ok_at = datetime.now()

            header = data.get("header", {})
            competitions = header.get("competitions", [{}])
            if competitions:
                status = competitions[0].get("status", {})
                game["state"] = status.get("type", {}).get("state", game.get("state"))
                game["detail"] = status.get("type", {}).get("detail", game.get("detail"))

            scores = get_scores(data, sport)

            situation = {}
            if sport == "nfl":
                situation = data.get("situation", {})
                if not situation:
                    drives = data.get("drives", {})
                    current = drives.get("current", {})
                    plays = current.get("plays", []) if current else []
                    if plays:
                        last_play = plays[-1]
                        situation = {
                            "downDistanceText": last_play.get("end", {}).get("downDistanceText", ""),
                            "possession": last_play.get("end", {}).get("team", {}).get("abbreviation", ""),
                        }

            if sport == "nfl":
                new_plays = get_plays_nfl(data, self.last_play_ids)
            else:
                new_plays = get_plays_basketball(data, self.last_play_ids)

            if not self.initialized:
                self.static_log.append(f"[bold cyan]Monitoring:[/bold cyan] {game['away_name']} @ {game['home_name']}")
                self.static_log.append("[dim]Keys: Q quit | Space pause | F follow | / search | A all | S scores | P penalties | V turnovers[/dim]")
                self.static_log.append("")
                for play in new_plays[-10:]:
                    self._append_play(play)
                self.initialized = True
                self.last_scores = scores
            else:
                for play in new_plays:
                    self._append_play(play)
                    if play.get("scoring"):
                        notify(
                            f"{game['away_team']} vs {game['home_team']}",
                            f"SCORE! {scores[0]} - {scores[1]}",
                        )

                if scores != self.last_scores:
                    if scores[0] > self.last_scores[0]:
                        notify(
                            f"{game['away_team']} Scores!",
                            f"{game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}",
                        )
                    elif scores[1] > self.last_scores[1]:
                        notify(
                            f"{game['home_team']} Scores!",
                            f"{game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}",
                        )
                self.last_scores = scores

            # Keep scrollback bounded
            max_keep = int((CONFIG or {}).get("max_scrollback", 1500) or 1500)
            if len(self.plays_log) > max_keep:
                self.plays_log = self.plays_log[:max_keep]

            header_panel = display_header(game, scores, situation, sport)
            leaders = build_leaders_table(data, game)
            self._render(header_panel=header_panel, leaders=leaders)

            if game.get("state") == "post":
                self.plays_log.insert(0, {"line": "", "raw": "", "tags": set()})
                self.plays_log.insert(0, {"line": "[bold yellow]FINAL[/bold yellow]", "raw": "final", "tags": {"score"}})
                self.plays_log.insert(0, {"line": f"[bold]{game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}[/bold]", "raw": "final", "tags": {"score"}})
                notify(
                    "Game Over",
                    f"Final: {game['away_team']} {scores[0]} - {scores[1]} {game['home_team']}",
                )
                self._finalized = True
                if self._timer:
                    self._timer.pause()

    GameMonitorApp().run()


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
        if (CONFIG or {}).get("tui_game_picker", True):
            selected_game = select_game_textual(games)
        else:
            selected_game = display_games(games)

    if not selected_game:
        console.print("[yellow]No game selected.[/yellow]")
        sys.exit(0)

    # Start monitoring
    monitor_game(selected_game, args.sport, args.refresh)


if __name__ == "__main__":
    main()
