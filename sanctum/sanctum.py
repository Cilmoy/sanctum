"""
sanctum.py — CLI entry point for the Sanctum equity screener.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, List

try:
    import yaml
except ImportError:
    print(
        "Error: required packages are not installed.\n"
        "Run: pip install -r requirements.txt"
    )
    sys.exit(1)

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich import box

from sanctum.data.cache import SanctumDB
from sanctum.data.fetcher import DataFetcher
from sanctum.data.watchlist import WatchlistManager
from sanctum.portfolio.manager import PortfolioManager
from sanctum.scoring.filters import apply_filters
from sanctum.scoring.composite import CompositeScorer
from sanctum.output.terminal import TerminalOutput, GOLD
from sanctum.output.tui import SanctumTUI

console = Console()


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(__file__).parent / path
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f)


def apply_overrides(config: dict, overrides: list[str]) -> dict:
    """Apply --set key.path=value overrides to config dict."""
    for override in overrides or []:
        key_path, sep, value = override.partition("=")
        if not sep:
            print(f"Error: invalid override '{override}' — expected KEY=VALUE format.")
            sys.exit(1)
        keys = key_path.strip().split(".")
        node = config
        try:
            for k in keys[:-1]:
                if k not in node:
                    node[k] = {}
                node = node[k]
        except (KeyError, TypeError) as e:
            print(f"Error: config key {e} not found or invalid in path '{key_path}'.")
            sys.exit(1)
        # Attempt numeric conversion
        try:
            if "." in value:
                value = float(value)
            else:
                value = int(value)
        except ValueError:
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
        node[keys[-1]] = value
    return config


def get_tickers(config: dict, args: argparse.Namespace, db: SanctumDB) -> list[str]:
    if hasattr(args, "tickers") and args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",")]
    
    source = config.get("universe", {}).get("source", "all_us")
    
    # Default screen to watchlist if no tickers provided and not specified otherwise
    if args.command == "screen" and not (hasattr(args, "tickers") and args.tickers):
        watchlist = WatchlistManager(db).list()
        if watchlist:
            return watchlist

    if source == "watchlist":
        return WatchlistManager(db).list()
    elif source == "custom":
        tickers = config.get("universe", {}).get("custom_tickers", [])
        if not tickers:
            print("No tickers specified. Use --tickers or set universe.custom_tickers in config.yaml.")
            sys.exit(1)
        return tickers
    elif source == "sp500":
        from sanctum.data.fetcher import fetch_sp500_tickers
        return fetch_sp500_tickers()
    elif source == "nasdaq100":
        from sanctum.data.fetcher import fetch_nasdaq100_tickers
        return fetch_nasdaq100_tickers()
    elif source == "all_us":
        from sanctum.data.fetcher import fetch_all_us_tickers
        return fetch_all_us_tickers()
    else:
        print(f"Error: unknown universe source '{source}'. Valid options: all_us, sp500, nasdaq100, custom, watchlist.")
        sys.exit(1)


def cmd_init(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    output = TerminalOutput(config)
    output.print_header("Initialization")
    console.print("\n[bold]Welcome to Sanctum CLI.[/bold]")
    console.print("This tool performs advanced equity screening and analysis using WACC, DCF, and Bayesian models.")
    console.print(f"Database: [cyan]{config.get('cache', {}).get('db_path', '.cache/sanctum.db')}[/cyan]")
    
    if Confirm.ask("\nInitialize persistent storage now?"):
        # SanctumDB(config) already called in main(), so tables exist.
        console.print("[green]✔[/green] Persistent storage initialized successfully.")
    else:
        console.print("[yellow]Initialization skipped.[/yellow]")


def cmd_watchlist(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    manager = WatchlistManager(db)
    
    if args.action == "add":
        ticker = args.ticker or Prompt.ask("Enter ticker to add")
        manager.add(ticker)
        console.print(f"[green]✔[/green] Added [bold]{ticker.upper()}[/bold] to watchlist.")
    elif args.action == "remove":
        ticker = args.ticker or Prompt.ask("Enter ticker to remove")
        manager.remove(ticker)
        console.print(f"[red]✔[/red] Removed [bold]{ticker.upper()}[/bold] from watchlist.")
    elif args.action == "list" or not args.action:
        tickers = manager.list()
        if not tickers:
            console.print("[yellow]Watchlist is empty.[/yellow]")
        else:
            table = Table(title="Watchlist", box=box.SIMPLE_HEAD, header_style=f"bold {GOLD}")
            table.add_column("Ticker", style=f"bold {GOLD}")
            for t in tickers:
                table.add_row(t)
            console.print(table)


def cmd_portfolio(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    manager = PortfolioManager(db)
    output = TerminalOutput(config)
    
    if args.action == "add":
        ticker = args.ticker or Prompt.ask("Enter ticker")
        shares = args.shares if args.shares is not None else float(Prompt.ask("Enter shares", default="0"))
        avg_cost = args.avg_cost if args.avg_cost is not None else float(Prompt.ask("Enter average cost", default="0"))
        manager.add(ticker, shares, avg_cost)
        console.print(f"[green]✔[/green] Added [bold]{ticker.upper()}[/bold] to portfolio.")
    elif args.action == "remove":
        ticker = args.ticker or Prompt.ask("Enter ticker to remove")
        manager.remove(ticker)
        console.print(f"[red]✔[/red] Removed [bold]{ticker.upper()}[/bold] from portfolio.")
    elif args.action == "show" or not args.action:
        holdings_list = manager.list()
        if not holdings_list:
            console.print("[yellow]Portfolio is empty.[/yellow]")
            return

        holdings = {h["ticker"]: h["shares"] for h in holdings_list}
        tickers = list(holdings.keys())

        fetcher = DataFetcher(config, db=db)
        scorer = CompositeScorer(config, mode="analyze")
        
        scored = []
        with output.status_spinner("Analyzing portfolio..."):
            for ticker in tickers:
                stock = fetcher.fetch_single(ticker)
                if stock:
                    scored.append(scorer.score_one(stock))

        output.print_portfolio(holdings, scored, [])
    elif args.action == "rebalance":
        from sanctum.portfolio.rebalance import suggest_rebalance
        holdings_list = manager.list()
        if not holdings_list:
            console.print("[yellow]Portfolio is empty.[/yellow]")
            return

        holdings = {h["ticker"]: h["shares"] for h in holdings_list}
        tickers = list(holdings.keys())

        fetcher = DataFetcher(config, db=db)
        scorer = CompositeScorer(config, mode="analyze")
        
        scored = []
        with output.status_spinner("Analyzing for rebalance..."):
            for ticker in tickers:
                stock = fetcher.fetch_single(ticker)
                if stock:
                    scored.append(scorer.score_one(stock))

        suggestions = suggest_rebalance(holdings, scored, config)
        output.print_portfolio(holdings, scored, suggestions)


def cmd_screen(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    tickers = get_tickers(config, args, db)
    fetcher = DataFetcher(config, db=db)
    output = TerminalOutput(config)

    output.print_header(f"Screening {len(tickers)} tickers")

    with output.status_spinner(f"Fetching and filtering {len(tickers)} stocks..."):
        stocks = fetcher.fetch_bulk(tickers)
        stocks = apply_filters(stocks, config)

    with output.status_spinner("Scoring universe..."):
        scorer = CompositeScorer(config, mode="screen")
        results = scorer.score_all(stocks)
        results.sort(key=lambda r: r["score"], reverse=True)

    threshold = config.get("scoring", {}).get("shortlist_threshold", 60)
    shortlisted = [r for r in results if r["score"] >= threshold]

    output.print_screen_results(results, shortlisted)

    if hasattr(args, "export") and args.export == "pdf":
        try:
            from sanctum.output.pdf_report import PDFReport
            PDFReport(config).generate_screen(results, shortlisted)
        except ImportError:
            console.print("[red]Error: PDF export requires 'reportlab'.[/red]")


def cmd_analyze(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    ticker = (args.ticker or Prompt.ask("Enter ticker")).upper()
    fetcher = DataFetcher(config, db=db)
    output = TerminalOutput(config)

    output.print_header(f"Deep Dive: {ticker}")

    with output.status_spinner(f"Performing deep-dive analysis for {ticker}..."):
        stock = fetcher.fetch_single(ticker, full=True)
        if stock is None:
            console.print(f"[red]Error: Failed to fetch data for {ticker}.[/red]")
            return

        scorer = CompositeScorer(config, mode="analyze")
        result = scorer.score_one(stock)

    output.print_analysis(result, show_math=config.get("output", {}).get("show_math", True))

    if hasattr(args, "export") and args.export == "pdf":
        try:
            from sanctum.output.pdf_report import PDFReport
            PDFReport(config).generate_analysis(result)
        except ImportError:
            console.print("[red]Error: PDF export requires 'reportlab'.[/red]")


def cmd_compare(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    tickers = [t.upper() for t in args.tickers]
    fetcher = DataFetcher(config, db=db)
    output = TerminalOutput(config)

    output.print_header(f"Comparing: {' vs '.join(tickers)}")

    scorer = CompositeScorer(config, mode="analyze")
    results = []
    
    with output.status_spinner(f"Analyzing {len(tickers)} tickers..."):
        for ticker in tickers:
            stock = fetcher.fetch_single(ticker)
            if stock is None:
                console.print(f"[yellow]Warning: could not fetch {ticker}, skipping.[/yellow]")
                continue
            results.append(scorer.score_one(stock))

    output.print_comparison(results)


def cmd_help(args: argparse.Namespace, config: dict, db: SanctumDB) -> None:
    TerminalOutput(config).print_help(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sanctum",
        description="SANCTUM LLC — Equity Screening & Analysis CLI",
        add_help=False
    )
    parser.add_argument("-h", "--help", action="store_true", help="Show help message and exit")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        metavar="KEY=VALUE",
        help="Override a config parameter",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and show full traces",
    )

    subparsers = parser.add_subparsers(dest="command")

    # init
    subparsers.add_parser("init", help="Initialize persistent storage")

    # watchlist
    watchlist_p = subparsers.add_parser("watchlist", help="Manage watchlist")
    watchlist_sub = watchlist_p.add_subparsers(dest="action")
    
    wl_add = watchlist_sub.add_parser("add", help="Add ticker to watchlist")
    wl_add.add_argument("ticker", nargs="?", help="Ticker symbol")
    
    wl_rem = watchlist_sub.add_parser("remove", help="Remove ticker from watchlist")
    wl_rem.add_argument("ticker", nargs="?", help="Ticker symbol")
    
    watchlist_sub.add_parser("list", help="List watchlist")

    # portfolio
    portfolio_p = subparsers.add_parser("portfolio", help="Manage portfolio")
    port_sub = portfolio_p.add_subparsers(dest="action")
    
    p_add = port_sub.add_parser("add", help="Add holding")
    p_add.add_argument("ticker", nargs="?", help="Ticker symbol")
    p_add.add_argument("shares", type=float, nargs="?", help="Number of shares")
    p_add.add_argument("avg_cost", type=float, nargs="?", help="Average cost basis")
    
    p_rem = port_sub.add_parser("remove", help="Remove holding")
    p_rem.add_argument("ticker", nargs="?", help="Ticker symbol")
    
    port_sub.add_parser("show", help="Show portfolio summary")
    port_sub.add_parser("rebalance", help="Show rebalancing suggestions")

    # screen
    screen_p = subparsers.add_parser("screen", help="Screen universe")
    screen_p.add_argument("--tickers", help="Comma-separated ticker list")
    screen_p.add_argument("--export", choices=["pdf"], help="Export results")

    # analyze
    analyze_p = subparsers.add_parser("analyze", help="Deep-dive a single stock")
    analyze_p.add_argument("ticker", nargs="?", help="Ticker symbol")
    analyze_p.add_argument("--export", choices=["pdf"], help="Export report")

    # compare
    compare_p = subparsers.add_parser("compare", help="Head-to-head comparison")
    compare_p.add_argument("tickers", nargs="+", help="Ticker symbols")

    # help
    subparsers.add_parser("help", help="Show help dashboard")

    return parser


def main() -> None:
    parser = build_parser()
    args, unknown = parser.parse_known_args()

    # Log level
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    config = load_config()
    config = apply_overrides(config, args.overrides)

    db = SanctumDB(config)

    if not args.command and not args.help:
        # Launch TUI on no args
        try:
            tui = SanctumTUI(config, db)
            tui.run()
        except Exception as e:
            console.print(f"[bold red]TUI Crash:[/bold red] {e}")
            if args.debug:
                logging.exception("TUI failed")
        finally:
            db.close()
        return

    if args.help or args.command == "help":
        TerminalOutput(config).print_help(config)
        db.close()
        return


    cmd_map = {
        "init": cmd_init,
        "watchlist": cmd_watchlist,
        "portfolio": cmd_portfolio,
        "screen": cmd_screen,
        "analyze": cmd_analyze,
        "compare": cmd_compare,
    }

    if args.command in cmd_map:
        try:
            cmd_map[args.command](args, config, db)
        except Exception as e:
            if args.debug:
                logging.exception("Command failed")
            else:
                console.print(f"[bold red]Error:[/bold red] {e}")
    else:
        parser.print_help()

    db.close()


if __name__ == "__main__":
    main()
