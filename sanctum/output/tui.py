"""
tui.py — Sanctum TUI built on Rich Live.

Navigation:  Ctrl-Q Quit  H Home  S Screen  A Analyze  C Compare  W Watchlist  P Portfolio  ? Help
Scrolling:   ↑ ↓ arrow keys  (trackpad scroll → arrow keys once mouse reporting is disabled)
Input:       type at prompt shown in body panel; ENTER confirm, ESC cancel
"""

import fcntl
import io
import logging
import os
import select
import sys
import termios
import time
import signal
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from data.fetcher import DataFetcher
from data.watchlist import WatchlistManager
from output.terminal import DIM, GOLD, TerminalOutput
from portfolio.manager import PortfolioManager
from scoring.composite import CompositeScorer
from scoring.filters import apply_filters

# Pre-compiled ticker regex for zero-allocation extraction
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")

# Enable SGR Mouse Reporting (1006) for scrollwheel and click support.
_MOUSE_ON  = "\x1b[?1000h\x1b[?1006h"
_MOUSE_OFF = "\x1b[?1000l\x1b[?1006l"

# Scroll delta per arrow / page key (lines).
_SCROLL = {
    "\x1b[A": -3, 
    "\x1b[B": 3, 
    "\x1b[5~": -20, 
    "\x1b[6~": 20,
    "MOUSE_UP": -5,
    "MOUSE_DOWN": 5
}

_SCROLLABLE = {"screen", "analyze", "compare", "help", "portfolio"}


# ── Key-buffer helpers (module-level, no state) ────────────────────────────

def _parse_key(buf: bytes) -> tuple[Optional[str], bytes]:
    """
    Extract exactly one key or escape sequence from *buf*.
    Returns (key_string, remaining_bytes).
    """
    if not buf:
        return None, buf

    b0 = buf[0]

    # ── Non-escape ────────────────────────────────────────────────────────
    if b0 != 0x1b:
        ch = chr(b0)
        remaining = buf[1:]
        # Accept printable, Enter, Backspace, Del, Ctrl-Q
        if b0 >= 32 or ch in "\r\n\x7f\x08\x11":
            return ch, remaining
        return None, remaining  # discard other controls

    # ── Escape sequence ───────────────────────────────────────────────────
    if len(buf) < 2:
        return None, buf

    b1 = buf[1]

    # CSI  ESC [
    if b1 == ord("["):
        if len(buf) < 3:
            return None, buf

        b2 = buf[2]

        # ── Mouse events — return MOUSE_UP/DOWN for scroll events ─────
        if b2 == ord("<"):
            end = -1
            for i in range(3, min(len(buf), 32)):
                if buf[i] in (ord("M"), ord("m")):
                    end = i
                    break
            if end == -1:
                return None, buf
            
            # SGR Format: ESC [ < Pb ; Px ; Py M/m
            try:
                seq = buf[3:end].decode("ascii")
                parts = seq.split(";")
                if not parts: return None, buf[end+1:]

                # Scroll wheel
                if parts[0] == "64": return "MOUSE_UP", buf[end+1:]
                if parts[0] == "65": return "MOUSE_DOWN", buf[end+1:]

                # Left-click PRESS only (seq ends in 'M')
                if parts[0] == "0" and buf[end] == ord("M"):
                    if len(parts) >= 3:
                        return f"CLICK_{parts[1]}_{parts[2]}", buf[end+1:]

            except Exception:
                pass
            
            return None, buf[end+1:]

        # ── X10 mouse: ESC [ M <3 raw bytes> — consume all 6, return nothing ──
        if b2 == ord("M"):
            if len(buf) < 6:
                return None, buf  # wait for all 6 bytes
            return None, buf[6:]

        # ── Arrow keys, Home, End ────────────────────────────────────
        if b2 in (ord("A"), ord("B"), ord("C"), ord("D"), ord("H"), ord("F")):
            return buf[:3].decode("latin-1"), buf[3:]

        # ── Page-up/down, Ins, Del, Home, End (with ~) ──────────────
        if b2 in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6")):
            if len(buf) < 4:
                return None, buf
            if buf[3] == ord("~"):
                return buf[:4].decode("latin-1"), buf[4:]
            
            # Modified keys (e.g. ESC[5;2~) — find terminal ~
            end = buf.find(ord("~"), 3)
            if end >= 0:
                return buf[:end+1].decode("latin-1", errors="replace"), buf[end+1:]

        # ── Other CSI sequences (F-keys etc.) ─────────────────────────
        for i in range(2, min(len(buf), 64)):
            c = buf[i]
            # Termination characters for CSI: 64-126 (@ to ~)
            if 64 <= c <= 126:
                return buf[:i+1].decode("latin-1", errors="replace"), buf[i+1:]
        
        if len(buf) >= 64:
            return None, buf[64:]
        return None, buf

    # Bare ESC
    return "\x1b", buf[1:]


class SanctumTUI:
    def __init__(self, config: dict, db):
        self.config = config
        self.db = db
        self.console = Console(file=sys.stdout)
        self.terminal = TerminalOutput(config)
        self.layout = self._make_layout()
        self.current_screen = "home"
        self.previous_screen = "home"
        self.running = True
        self.status_message = "Ready"

        # ── Data state ──────────────────────────────────────────────────
        self.screen_results: list = []
        self.shortlisted: list = []
        self.analysis_result: Optional[dict] = None
        self.comparison_results: list = []
        self.watchlist_tickers: list = []
        self.portfolio_data: dict = {}

        # ── Input state ─────────────────────────────────────────────────
        self.input_active = False
        self.input_buffer = ""
        self.input_prompt = ""
        self.input_callback: Optional[Callable] = None

        # ── Threading / refresh ─────────────────────────────────────────
        self.is_busy = False
        self._needs_refresh = False

        # ── Key buffer ──────────────────────────────────────────────────
        self._key_buf = b""

        # ── Scrollable body ──────────────────────────────────────────────
        self._scrollable_lines: list = []
        self._scroll_offset = 0
        self._rendered_id: Optional[str] = None

        # ── Progress counters ────────────────────────────────────────────
        self._fetch_done = 0
        self._fetch_total = 0
        self._score_done = 0
        self._score_total = 0
        self._phase_start: float = 0.0

        self._executor = ThreadPoolExecutor(max_workers=1)
        self.params_md = self._load_params_md()

    # ── Setup ─────────────────────────────────────────────────────────

    def _load_params_md(self) -> str:
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "PARAMETERS.md")
            with open(path) as f:
                return f.read()
        except Exception:
            return "# Error: Could not load PARAMETERS.md"

    def _make_layout(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(
            Layout(name="side", size=20),
            Layout(name="body"),
        )
        return layout

    # ── Static panels ─────────────────────────────────────────────────

    def _get_header(self) -> Panel:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", ratio=1)
        dot = "  [yellow]●[/yellow]" if self.is_busy else ""
        grid.add_row(
            Text.from_markup(
                f" [bold {GOLD}]{self.config.get('output', {}).get('brand_name', 'SANCTUM LLC')}[/bold {GOLD}]{dot}"
            ),
            Text(datetime.now().strftime("%Y-%m-%d %H:%M:%S "), style="dim"),
        )
        return Panel(grid, style=GOLD)

    def _get_footer(self) -> Panel:
        if self.input_active:
            content = (
                f"[bold {GOLD}]{self.input_prompt}:[/bold {GOLD}] "
                f"[bold]{self.input_buffer}[/bold]█  "
                "[dim]ENTER confirm  ·  ESC cancel[/dim]"
            )
        else:
            # Common nav keys
            nav = "[bold]Ctrl-Q[/bold] Quit  [bold]H[/bold] Home  [bold]S[/bold] Screen  [bold]A[/bold] Analyze  [bold]C[/bold] Compare  [bold]W[/bold] Watchlist  [bold]P[/bold] Portfolio  [bold]?[/bold] Help"
            
            # Context-specific Quick Actions
            if self.current_screen == "analyze" and self.analysis_result:
                ticker = self.analysis_result.get("ticker", "?")
                actions = f"  |  [bold {GOLD}]Quick Actions ({ticker}):[/bold {GOLD}] [bold]+[/bold] Watch  [bold]-[/bold] Unwatch  [bold]P[/bold] Add Port  [bold]B[/bold] Back"
                content = nav + actions
            else:
                content = nav + "  [dim]↑↓ / MouseWheel scroll[/dim]"
                
        return Panel(Text.from_markup(content, justify="center"), style="dim")

    def _get_sidebar(self) -> Panel:
        menu = Table.grid(expand=True)
        menu.add_column()
        for key, label, screen in [
            ("H", "Home",      "home"),
            ("S", "Screen",    "screen"),
            ("A", "Analyze",   "analyze"),
            ("C", "Compare",   "compare"),
            ("W", "Watchlist", "watchlist"),
            ("P", "Portfolio", "portfolio"),
            ("?", "Help",      "help"),
        ]:
            style = f"bold {GOLD}" if self.current_screen == screen else "white"
            menu.add_row(Text(f" {key} {label}", style=style))
            menu.add_row("")
        return Panel(menu, title="Menu", border_style=GOLD)

    # ── Body builders ─────────────────────────────────────────────────

    def _body_home(self) -> Panel:
        return Panel(
            Text.from_markup(
                f"\nWelcome to [bold {GOLD}]Sanctum[/bold {GOLD}].\n\n"
                "Professional equity screening and analysis terminal.\n\n"
                "[dim]Hotkeys:[/dim]\n"
                f"  [bold {GOLD}]S[/bold {GOLD}]  Run Screener\n"
                f"  [bold {GOLD}]A[/bold {GOLD}]  Deep-Dive Analysis\n"
                f"  [bold {GOLD}]C[/bold {GOLD}]  Ticker Comparison\n"
                f"  [bold {GOLD}]W[/bold {GOLD}]  Manage Watchlist\n"
                f"  [bold {GOLD}]P[/bold {GOLD}]  Portfolio Overview\n\n"
                "[dim]↑↓ arrows or MouseWheel scroll any results screen.[/dim]\n"
                "[dim][bold]Ctrl-Q[/bold] to quit.[/dim]"
            ),
            title="Dashboard",
            border_style=GOLD,
        )

    def _body_screen(self) -> Panel:
        if not self.screen_results:
            return Panel(self._progress_group(), title="Screen", border_style=GOLD)
        rid = f"screen_{len(self.screen_results)}"
        if self._rendered_id != rid:
            self._set_scroll(
                self.terminal.get_screen_results_table(self.screen_results, self.shortlisted),
                rid,
            )
        n, s = len(self.screen_results), len(self.shortlisted)
        return self._scroll_panel(f"Screen  {n} scored  {s} shortlisted")

    def _body_analyze(self) -> Panel:
        if self.input_active:
            return Panel(
                Text.from_markup(
                    f"\n  [bold {GOLD}]{self.input_prompt}:[/bold {GOLD}] "
                    f"[bold]{self.input_buffer}[/bold]█\n\n"
                    "  [dim]Type ticker then ENTER.  ESC to cancel.[/dim]\n\n"
                    f"  [dim]Examples:  AAPL  NVDA  MSFT  TSM  GOOG[/dim]"
                ),
                title="Deep-Dive Analysis",
                border_style=GOLD,
            )
        if not self.analysis_result:
            return Panel(self._progress_group(), title="Analyzing…", border_style=GOLD)
        ticker = self.analysis_result.get("ticker", "?")
        rid = f"analyze_{ticker}"
        if self._rendered_id != rid:
            self._set_scroll(
                self.terminal.get_analysis_renderables(self.analysis_result, show_math=True),
                rid,
            )
        return self._scroll_panel(f"Analysis: {ticker}")

    def _body_compare(self) -> Panel:
        if self.input_active:
            return Panel(
                Text.from_markup(
                    f"\n  [bold {GOLD}]{self.input_prompt}:[/bold {GOLD}] "
                    f"[bold]{self.input_buffer}[/bold]█\n\n"
                    "  [dim]Comma-separated tickers then ENTER.  ESC to cancel.[/dim]\n\n"
                    "  [dim]Example:  NVDA, TSM, INTC[/dim]"
                ),
                title="Ticker Comparison",
                border_style=GOLD,
            )
        if not self.comparison_results:
            if self.is_busy:
                return Panel(self._progress_group(), title="Comparing…", border_style=GOLD)
            return Panel(
                Text("  Press C to compare tickers.", style="dim"),
                title="Ticker Comparison",
                border_style=GOLD,
            )
        rid = "compare_" + "_".join(r["ticker"] for r in self.comparison_results)
        if self._rendered_id != rid:
            self._set_scroll(self.terminal.get_comparison_table(self.comparison_results), rid)
        return self._scroll_panel("Comparison Results")

    def _body_watchlist(self) -> Panel:
        if not self.watchlist_tickers:
            return Panel(
                Text.from_markup(
                    "\n  Watchlist is empty.\n\n"
                    "  [dim]sanctum watchlist add AAPL[/dim]"
                ),
                title="Watchlist",
                border_style=GOLD,
            )
        table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {GOLD}")
        table.add_column("Ticker", style=f"bold {GOLD}")
        for t in self.watchlist_tickers:
            table.add_row(t)
        note = Text.from_markup(
            "\n  [dim]Add: sanctum watchlist add TICKER"
            "  ·  Remove: sanctum watchlist remove TICKER[/dim]"
        )
        return Panel(Group(table, note), title="Watchlist", border_style=GOLD)

    def _body_portfolio(self) -> Panel:
        if not self.portfolio_data:
            if self.is_busy:
                return Panel(self._progress_group(), title="Portfolio", border_style=GOLD)
            return Panel(
                Text.from_markup(
                    "\n  Portfolio empty.\n\n"
                    "  [dim]sanctum portfolio add AAPL 10 180.00[/dim]"
                ),
                title="Portfolio",
                border_style=GOLD,
            )
        rid = "portfolio"
        if self._rendered_id != rid:
            self._set_scroll(
                self.terminal.get_portfolio_renderable(
                    self.portfolio_data["holdings"],
                    self.portfolio_data["scored"],
                    self.portfolio_data["suggestions"],
                ),
                rid,
            )
        return self._scroll_panel("Portfolio")

    def _body_help(self) -> Panel:
        rid = "help"
        if self._rendered_id != rid:
            self._set_scroll(
                Group(
                    self.terminal.get_help_renderables(self.config),
                    Text("\n" + "─" * 60 + "\n", style=DIM),
                    Text("PARAMETER DOCUMENTATION", style=f"bold {GOLD} underline"),
                    Markdown(self.params_md),
                ),
                rid,
            )
        return self._scroll_panel("Sanctum Manual")

    # ── Progress ──────────────────────────────────────────────────────

    def _progress_group(self) -> Group:
        els: list = [Text("")]
        if self._fetch_total > 0:
            els.append(self._pbar("Fetching", self._fetch_done, self._fetch_total, self._phase_start))
        if self._score_total > 0:
            els.append(self._pbar("Scoring ", self._score_done, self._score_total))
        if self._fetch_total == 0:
            els.append(Spinner("dots", text=f"  {self.status_message}"))
        else:
            els.append(Text(f"  {self.status_message}", style="dim"))
        els.append(Text.from_markup("\n  [dim]Press H to cancel.[/dim]"))
        return Group(*els)

    @staticmethod
    def _pbar(label: str, done: int, total: int, t0: Optional[float] = None, w: int = 28) -> Text:
        pct = done / total if total else 0
        bar = "█" * int(w * pct) + "░" * (w - int(w * pct))
        eta = ""
        if t0 and done > 0:
            rem = (time.time() - t0) / done * (total - done)
            if rem > 5:
                m, s = divmod(int(rem), 60)
                eta = f"  ~{m}m{s:02d}s" if m else f"  ~{s}s"
            else:
                eta = "  finishing…"
        return Text.from_markup(
            f"  [bold]{label}:[/bold]  [{GOLD}]{bar}[/{GOLD}]  [dim]{done}/{total}[/dim]{eta}"
        )

    # ── Scrollable body ────────────────────────────────────────────────

    def _capture(self, renderable) -> list:
        # Precision width calculation: console_width - sidebar(20) - panel_border(2) - inner_padding(2)
        w = max(40, (self.console.width or 120) - 24)
        buf = io.StringIO()
        cap = Console(file=buf, width=w,
                      highlight=False, color_system="truecolor")
        cap.print(renderable)
        return buf.getvalue().split("\n")

    def _set_scroll(self, renderable, rid: str) -> None:
        self._scrollable_lines = self._capture(renderable)
        self._scroll_offset = 0
        self._rendered_id = rid

    def _scroll_panel(self, title: str = "") -> Panel:
        lines = self._scrollable_lines
        total = len(lines)
        # Accurate height: console_height - header(3) - footer(3) - panel_border(2)
        vh = max(5, (self.console.height or 40) - 8)
        
        # Ensure scroll offset is within valid range
        self._scroll_offset = max(0, min(self._scroll_offset, max(0, total - vh)))
        
        visible = lines[self._scroll_offset : self._scroll_offset + vh]
        # Pad with empty lines if content is shorter than viewport to prevent layout jumping
        if len(visible) < vh:
            visible.extend([""] * (vh - len(visible)))
            
        content = Text.from_ansi("\n".join(visible))
        hint = ""
        if total > vh:
            pct = min(100, int(100 * (self._scroll_offset + vh) / total))
            hint = f"  [dim]↕ {pct}%[/dim]"
        return Panel(content, title=Text.from_markup(f"{title}{hint}"), border_style=GOLD)

    def _clear_scroll(self) -> None:
        self._scrollable_lines = []
        self._scroll_offset = 0
        self._rendered_id = None

    # ── Layout ────────────────────────────────────────────────────────

    def _update_header(self) -> None:
        self.layout["header"].update(self._get_header())

    def _update_footer(self) -> None:
        self.layout["footer"].update(self._get_footer())

    def _update_sidebar(self) -> None:
        self.layout["side"].update(self._get_sidebar())

    def _update_body(self) -> None:
        body_fn = {
            "home":      self._body_home,
            "screen":    self._body_screen,
            "analyze":   self._body_analyze,
            "compare":   self._body_compare,
            "watchlist": self._body_watchlist,
            "portfolio": self._body_portfolio,
            "help":      self._body_help,
        }.get(self.current_screen, self._body_home)
        self.layout["body"].update(body_fn())

    def _update_layout(self) -> None:
        """Rebuild all panels. Call from main thread only."""
        self._update_header()
        self._update_footer()
        self._update_sidebar()
        self._update_body()

    def _update_input_fast(self) -> None:
        """Cheap rebuild for input keystrokes: footer + body only."""
        self.layout["footer"].update(self._get_footer())
        fn = {"analyze": self._body_analyze, "compare": self._body_compare}.get(
            self.current_screen
        )
        if fn:
            self.layout["body"].update(fn())

    # ── Key reading ───────────────────────────────────────────────────

    def _get_key(self) -> Optional[str]:
        """
        Non-blocking check of the key buffer. 
        Raw mode setup is now handled at the app-level in run() to eliminate lag.
        """
        fd = sys.stdin.fileno()
        
        # Drain stdin into self._key_buf (non-blocking after 5ms wait for snappiness)
        if self._key_buf or select.select([sys.stdin], [], [], 0.005)[0]:
            old_fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, old_fl | os.O_NONBLOCK)
            try:
                while True:
                    try:
                        chunk = os.read(fd, 256)
                        if not chunk:
                            break
                        self._key_buf += chunk
                    except BlockingIOError:
                        break
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, old_fl)

        # If we just got an ESC at the tail with nothing following,
        # give the terminal 15 ms to send the rest of the sequence.
        if self._key_buf and self._key_buf[-1:] == b"\x1b":
            if len(self._key_buf) == 1 or self._key_buf[-2:-1] != b"[":
                time.sleep(0.015)
                old_fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, old_fl | os.O_NONBLOCK)
                try:
                    try:
                        self._key_buf += os.read(fd, 64)
                    except BlockingIOError:
                        pass
                finally:
                    fcntl.fcntl(fd, fcntl.F_SETFL, old_fl)

        # Parse one key from the front of the buffer
        key, self._key_buf = _parse_key(self._key_buf)
        return key

    def _handle_input(self, key: str) -> None:
        if key in ("\r", "\n"):
            cb, val = self.input_callback, self.input_buffer.strip()
            self.input_active = False
            self.input_buffer = ""
            self.input_callback = None
            if cb and val:
                cb(val)
        elif key in ("\x7f", "\x08"):
            self.input_buffer = self.input_buffer[:-1]
        elif key == "\x1b":
            self.input_active = False
            self.input_buffer = ""
            self.input_callback = None
        elif len(key) == 1 and ord(key) >= 32:
            self.input_buffer += key

    # ── Commands ──────────────────────────────────────────────────────

    def _trigger(self, func: Callable) -> None:
        if self.is_busy: return
        def wrapper() -> None:
            self.is_busy = True
            try: func()
            except Exception as exc:
                logging.exception(f"Background task failed: {exc}")
                self.status_message = f"Error: {exc}"
            finally:
                self.is_busy = False
                self._needs_refresh = True
        self._executor.submit(wrapper)

    def _resolve_tickers(self) -> list:
        source = self.config.get("universe", {}).get("source", "watchlist")
        tickers: list = []
        if source == "watchlist":
            tickers = WatchlistManager(self.db).list()
        elif source == "sp500":
            from data.fetcher import fetch_sp500_tickers
            tickers = fetch_sp500_tickers()
        elif source == "nasdaq100":
            from data.fetcher import fetch_nasdaq100_tickers
            tickers = fetch_nasdaq100_tickers()
        elif source == "all_us":
            from data.fetcher import fetch_all_us_tickers
            tickers = fetch_all_us_tickers()
        elif source == "custom":
            tickers = self.config.get("universe", {}).get("custom_tickers", [])
        if not tickers:
            tickers = ["AAPL", "MSFT", "GOOG", "NVDA", "TSLA", "TSM"]
        return tickers

    def _reset_progress(self):
        self._fetch_done = 0
        self._fetch_total = 0
        self._score_done = 0
        self._score_total = 0
        self.status_message = "Ready"

    def _cmd_screen(self) -> None:
        self.previous_screen = self.current_screen
        self.current_screen = "screen"; self.screen_results = []; self._clear_scroll()
        self._reset_progress()
        def run() -> None:
            self._phase_start = time.time()
            fetcher = DataFetcher(self.config, db=self.db)
            tickers = self._resolve_tickers()
            self._fetch_total = len(tickers)
            self.status_message = f"Fetching {self._fetch_total} stocks..."
            
            def on_complete(ticker, done, total):
                self._fetch_done = done
                self.status_message = f"Fetched {ticker} ({done}/{total})"
                self._needs_refresh = True

            # ── Step 1: Parallel Fetch ────────────────────────────────────────
            stocks = fetcher.fetch_bulk(tickers, on_ticker_complete=on_complete)
            
            # ── Step 2: Filter ───────────────────────────────────────────────
            self.status_message = "Filtering..."
            self._needs_refresh = True
            stocks = apply_filters(stocks, self.config)
            
            # ── Step 3: Parallel Scoring ─────────────────────────────────────
            self._score_total = len(stocks)
            self.status_message = f"Scoring {self._score_total} stocks..."
            self._needs_refresh = True
            scorer = CompositeScorer(self.config, mode="screen")
            
            results = []
            with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
                future_to_stock = {executor.submit(scorer.score_one, s): s for s in stocks}
                for i, future in enumerate(as_completed(future_to_stock), 1):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        s = future_to_stock[future]
                        logger.error(f"{s.ticker} scoring failed: {e}")
                    self._score_done = i
                    self._needs_refresh = True
            
            results.sort(key=lambda r: r["score"], reverse=True)
            self.screen_results = results
            threshold = self.config.get("scoring", {}).get("shortlist_threshold", 60)
            self.shortlisted = [r for r in results if r["score"] >= threshold]
            self.status_message = f"Done ({time.time() - self._phase_start:.1f}s)"
            self._needs_refresh = True
            
        self._trigger(run)

    def _cmd_analyze(self, ticker: Optional[str] = None) -> None:
        if not ticker:
            self.input_active = True; self.input_prompt = "Ticker"
            self.input_callback = self._cmd_analyze; self.current_screen = "analyze"
            self.analysis_result = None; self._clear_scroll(); return
        
        self.previous_screen = self.current_screen
        self.current_screen = "analyze"; self.analysis_result = None; self._clear_scroll()
        self._reset_progress()
        
        def run() -> None:
            self.status_message = f"Analyzing {ticker.upper()}..."
            fetcher = DataFetcher(self.config, db=self.db)
            stock = fetcher.fetch_single(ticker.upper(), full=True)
            if stock:
                self.analysis_result = CompositeScorer(self.config, mode="analyze").score_one(stock)
                self.status_message = "Done"
            else:
                self.status_message = f"Failed to fetch {ticker}"
            self._needs_refresh = True
        
        self._trigger(run)

    def _cmd_compare(self, ts: Optional[str] = None) -> None:
        if not ts:
            self.input_active = True; self.input_prompt = "Tickers (CSV)"
            self.input_callback = self._cmd_compare; self.current_screen = "compare"
            self.comparison_results = []; self._clear_scroll(); return
        
        self.previous_screen = self.current_screen
        self.current_screen = "compare"; self.comparison_results = []; self._clear_scroll()
        self._reset_progress()
        
        def run() -> None:
            self.status_message = "Comparing stocks..."
            fetcher = DataFetcher(self.config, db=self.db)
            scorer = CompositeScorer(self.config)
            self.comparison_results = [scorer.score_one(fetcher.fetch_single(t)) for t in ts.split(",") if t.strip()]
            self.status_message = "Done"
            self._needs_refresh = True
            
        self._trigger(run)

    def _cmd_watchlist(self) -> None:
        self.previous_screen = self.current_screen
        self.current_screen = "watchlist"; self._clear_scroll()
        self._reset_progress()
        self.watchlist_tickers = WatchlistManager(self.db).list()

    def _cmd_portfolio(self) -> None:
        self.previous_screen = self.current_screen
        self.current_screen = "portfolio"; self.portfolio_data = {}; self._clear_scroll()
        self._reset_progress()
        def run() -> None:
            self.status_message = "Loading portfolio..."
            manager = PortfolioManager(self.db)
            holdings = {h["ticker"]: h["shares"] for h in manager.list()}
            fetcher = DataFetcher(self.config, db=self.db)
            scorer = CompositeScorer(self.config)
            scored = [scorer.score_one(fetcher.fetch_single(t)) for t in holdings]
            from portfolio.rebalance import suggest_rebalance
            self.portfolio_data = {"holdings": holdings, "scored": scored, "suggestions": suggest_rebalance(holdings, scored, self.config)}
            self.status_message = "Done"
            self._needs_refresh = True
        self._trigger(run)

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Main TUI loop. Refactored for rock-solid initialization and terminal stability.
        """
        # 1. Manage noise by controlling the log level
        logging.getLogger().setLevel(logging.CRITICAL)
        
        # 2. Initialize console — force_terminal=True is critical: without it, Console()
        # may return is_terminal=False on some macOS shells, causing Live to skip the
        # alternate screen and stack every refresh on top of the previous one.
        self.console = Console(force_terminal=True)
        fd = sys.stdin.fileno()

        # Setup WINCH handler for terminal resizing
        def _on_resize(*_):
            self._rendered_id = None # Force re-capture for new width
            self._needs_refresh = True
        signal.signal(signal.SIGWINCH, _on_resize)

        # 3. Enter TUI mode and manage state via try...finally
        try:
            # Enable Mouse Reporting
            sys.stdout.write(_MOUSE_ON)
            sys.stdout.flush()

            # Save terminal settings and enter raw input mode.
            # We do NOT use tty.setraw() because it also clears OPOST (output
            # post-processing), which prevents \n → \r\n conversion and causes
            # Rich's output to staircase to the right instead of wrapping cleanly.
            old_attr = termios.tcgetattr(fd)
            mode = termios.tcgetattr(fd)
            mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK |
                         termios.ISTRIP | termios.IXON)
            # mode[1] (output flags) intentionally NOT modified — preserves OPOST
            mode[2] &= ~(termios.CSIZE | termios.PARENB)
            mode[2] |= termios.CS8
            mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
            mode[6][termios.VMIN] = 1
            mode[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSADRAIN, mode)
            
            # Initial layout build
            self._update_layout()
            
            # 4. Use rich's standard approach: screen=True handles the alternate buffer
            with Live(self.layout, console=self.console, screen=True, auto_refresh=False) as live:
                live.refresh()
                _last_busy_refresh = time.monotonic()

                while self.running:
                    # ── Background state changes ──
                    if self._needs_refresh:
                        self._needs_refresh = False
                        try:
                            self._update_layout()
                        except Exception:
                            pass
                        live.refresh()

                    # ── Get next key ──
                    key = self._get_key()
                    if not key:
                        if self.is_busy:
                            now = time.monotonic()
                            if now - _last_busy_refresh >= 0.1:
                                try: self._update_body()
                                except Exception: pass
                                live.refresh()
                                _last_busy_refresh = now
                        else:
                            time.sleep(0.01)
                        continue

                    # ── Handle Input ──
                    k = key.lower()
                    
                    # Global Quit
                    if key == "\x11": # Ctrl-Q
                        self.running = False; break

                    # Global Input Buffer
                    if self.input_active:
                        self._handle_input(key)
                        self._update_input_fast()
                        live.refresh()
                        continue

                    # ── Mouse Interaction ──
                    if key.startswith("CLICK_"):
                        try:
                            _, x_str, y_str = key.split("_")
                            x, y = int(x_str), int(y_str)
                            
                            # Senior SWE: Fuzzy vertical search. 
                            # Different terminals/fonts have slight padding offsets.
                            # We check the clicked line and ±1 neighbor.
                            base_idx = y - 5 + self._scroll_offset
                            for offset in [-1, 0, 1]:
                                row_idx = base_idx + offset
                                if self.current_screen == "screen" and 0 <= row_idx < len(self._scrollable_lines):
                                    line = self._scrollable_lines[row_idx]
                                    found = _TICKER_RE.findall(line)
                                    if found:
                                        # Only trigger if we find a ticker that is actually in our results
                                        # (prevents clicking 'SCORE' or 'WACC' in header)
                                        for candidate in found:
                                            if any(candidate == r["ticker"] for r in self.screen_results):
                                                self._cmd_analyze(candidate)
                                                break
                                        else: continue # only executed if inner loop didn't break
                                        break # break middle loop
                        except Exception: pass
                        continue # continue outer while loop

                    # ── Analyze Screen Specifics ──
                    elif self.current_screen == "analyze" and self.analysis_result and key in ("+", "-", "p", "P", "b", "B"):
                        ticker = self.analysis_result.get("ticker")
                        if key == "+":
                            WatchlistManager(self.db).add(ticker)
                            self.status_message = f"Added {ticker} to watchlist"
                        elif key == "-":
                            WatchlistManager(self.db).remove(ticker)
                            self.status_message = f"Removed {ticker} from watchlist"
                        elif k == "p":
                            self.input_active = True
                            self.input_prompt = f"Add {ticker} to Portfolio (shares cost)"
                            def portfolio_cb(val, _t=ticker):
                                try:
                                    parts = val.split()
                                    s, c = float(parts[0]), float(parts[1])
                                    PortfolioManager(self.db).add(_t, s, c)
                                    self.status_message = f"Added {s} {_t} @ ${c}"
                                except Exception:
                                    self.status_message = "Invalid input (shares cost)"
                                self._needs_refresh = True
                            self.input_callback = portfolio_cb
                        elif k == "b":
                            self.current_screen = "screen"; self._clear_scroll()

                    # ── Scrolling ──
                    elif key in _SCROLL and self.current_screen in _SCROLLABLE:
                        self._scroll_offset = max(0, self._scroll_offset + _SCROLL[key])

                    # ── Global Navigation ──
                    elif k == "h": self.previous_screen = self.current_screen; self.current_screen = "home"; self._clear_scroll()
                    elif k == "s": self._cmd_screen()
                    elif k == "a": self._cmd_analyze()
                    elif k == "c": self.previous_screen = self.current_screen; self._cmd_compare()
                    elif k == "w": self.previous_screen = self.current_screen; self._cmd_watchlist()
                    elif k == "p": self.previous_screen = self.current_screen; self._cmd_portfolio()
                    elif key == "?": self.previous_screen = self.current_screen; self.current_screen = "help"; self._clear_scroll()
                    elif key == "\x1b": # Esc defaults to home or back
                        if self.current_screen == "analyze": self.current_screen = "screen"
                        else: self.current_screen = "home"

                    # ── Final Update/Refresh ──
                    try:
                        self._update_layout()
                    except Exception: pass
                    live.refresh()
                    _last_busy_refresh = time.monotonic()
        except Exception:
            logging.exception("TUI Fatal Error")
            raise
        finally:
            # Restore terminal settings and disable mouse reporting
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
            sys.stdout.write(_MOUSE_OFF)
            sys.stdout.flush()
