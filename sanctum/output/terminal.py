"""
terminal.py — Rich terminal output for Sanctum.

Dark theme, gold accent. Tables should be clean and scannable.
"""

import logging
from typing import Any, List, Dict, Optional
from contextlib import contextmanager

from rich.console import Console, Group
from rich.table import Table
from rich import box
from rich.text import Text
from rich.panel import Panel
from rich.rule import Rule
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.columns import Columns
from rich.layout import Layout

logger = logging.getLogger(__name__)

GOLD = "#c9a84c"
DIM = "dim white"
UP_COLOR = "green"
DOWN_COLOR = "red"

console = Console()


def _upside_str(upside_pct: float) -> Text:
    """Format upside percentage with color."""
    sign = "+" if upside_pct >= 0 else ""
    color = UP_COLOR if upside_pct >= 0 else DOWN_COLOR
    return Text(f"{sign}{upside_pct:.1f}%", style=color)


def _score_color(score: float) -> str:
    if score >= 75:
        return "bold green"
    elif score >= 60:
        return GOLD
    elif score >= 40:
        return "yellow"
    return "dim red"


class TerminalOutput:

    def __init__(self, config: dict):
        self.config = config
        self.brand = config.get("output", {}).get("brand_name", "SANCTUM LLC")

    @contextmanager
    def status_spinner(self, message: str = "Working..."):
        with console.status(f"[bold {GOLD}]{message}[/bold {GOLD}]", spinner="dots"):
            yield

    def get_header(self, subtitle: str = "") -> Panel:
        title = f"[bold {GOLD}]{self.brand}[/bold {GOLD}]"
        if subtitle:
            title += f"\n[dim]{subtitle}[/dim]"
        return Panel(title, border_style=GOLD, expand=False)

    def print_header(self, subtitle: str = "") -> None:
        console.print(self.get_header(subtitle))

    def get_screen_results_table(self, results: list[dict], shortlisted: list[dict]) -> Group:
        top_n = self.config.get("output", {}).get("top_n", 20)
        display = results[:top_n]

        info = Text.from_markup(
            f"\n[{GOLD}]Universe:[/{GOLD}] {len(results)} scored  "
            f"[{GOLD}]Shortlisted:[/{GOLD}] {len(shortlisted)}\n"
        )

        table = Table(
            box=box.SQUARE,
            header_style=f"bold {GOLD}",
            show_lines=False,
            pad_edge=False,
            expand=True,
        )
        table.add_column("Rank", justify="right", style=DIM, width=4)
        table.add_column("Ticker", style=f"bold {GOLD}", ratio=1)
        table.add_column("Name", style=DIM, ratio=3)
        table.add_column("F.Score", justify="right", width=8)
        table.add_column("C.Score", justify="right", width=8)
        table.add_column("Archetype", style=DIM, ratio=2)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Upside", justify="right", width=10)
        table.add_column("Sector", style=DIM, ratio=2)

        for i, r in enumerate(display, 1):
            score = r.get("score", 0.0)
            cscore = r.get("catalyst_score", 50.0)
            upside = r.get("dcf_upside_pct", 0.0)
            price = r.get("current_price")
            sector = r.get("sector", "—")
            name = r.get("company_name") or "—"
            archetype = r.get("trade_archetype", "")
            # Shorten archetype for table
            arch_short = archetype.split("—")[0].strip() if "—" in archetype else archetype

            table.add_row(
                str(i),
                r.get("ticker", "?"),
                name,
                Text(f"{score:.1f}", style=_score_color(score)),
                Text(f"{cscore:.1f}", style=_score_color(cscore)),
                arch_short,
                f"${price:.2f}" if price else "—",
                _upside_str(upside),
                sector if sector else "—",
            )
        return Group(info, table)

    def print_screen_results(self, results: list[dict], shortlisted: list[dict]) -> None:
        console.print(self.get_screen_results_table(results, shortlisted))

    def get_analysis_renderables(self, result: dict, show_math: bool = True) -> Group:
        ticker = result.get("ticker", "?")
        name = result.get("company_name")
        title = f"{ticker}" + (f"  ({name})" if name else "")
        
        elements = [Rule(title, style=GOLD)]
        elements.append(self._get_stock_info_table(result))
        elements.append(self._get_summary_table(result))
        elements.append(self._get_score_breakdown_table(result))
        elements.append(self._get_catalyst_renderable(result))

        if show_math:
            elements.append(self._get_wacc_derivation_renderable(result))
            elements.append(self._get_dcf_table(result))
            elements.append(self._get_mc_table(result))
            elements.append(self._get_bayesian_trace_table(result))
            elements.append(self._get_sensitivity_table(result))

        elements.append(self._get_options_renderable(result))
        return Group(*[e for e in elements if e is not None])

    def print_analysis(self, result: dict, show_math: bool = True) -> None:
        console.print(self.get_analysis_renderables(result, show_math))

    def _get_stock_info_table(self, result: dict) -> Table:
        price = result.get("current_price")
        mktcap = result.get("market_cap")
        beta = result.get("beta")
        ma50 = result.get("ma_50")
        ma200 = result.get("ma_200")
        hi52 = result.get("week_52_high")
        lo52 = result.get("week_52_low")
        trail_pe = result.get("trailing_pe")
        fwd_pe = result.get("forward_pe")
        div_yield = result.get("dividend_yield")
        short_ratio = result.get("short_ratio")
        sector = result.get("sector", "—")
        industry = result.get("industry", "—")
        volume = result.get("avg_daily_volume")

        def _ma_str(ma: float) -> Text:
            if price and ma:
                diff = (price / ma - 1) * 100
                color = UP_COLOR if price >= ma else DOWN_COLOR
                sign = "+" if diff >= 0 else ""
                return Text(f"${ma:.2f}  ({sign}{diff:.1f}%)", style=color)
            return Text("—", style=DIM)

        def _mktcap_str(mc: float) -> str:
            if mc >= 1e12:
                return f"${mc/1e12:.2f}T"
            elif mc >= 1e9:
                return f"${mc/1e9:.1f}B"
            return f"${mc/1e6:.0f}M"

        t = Table(box=box.SQUARE, show_header=False, pad_edge=False, expand=True)
        t.add_column("Label", style=DIM, no_wrap=True, ratio=1)
        t.add_column("Value", no_wrap=True, ratio=2)
        t.add_column("Label2", style=DIM, no_wrap=True, ratio=1)
        t.add_column("Value2", no_wrap=True, ratio=2)

        rows = [
            ("Sector",       sector or "—",
             "Industry",     industry or "—"),
            ("Market Cap",   _mktcap_str(mktcap) if mktcap else "—",
             "Avg Volume",   f"{volume/1e6:.1f}M" if volume else "—"),
            ("52-Wk High",   f"${hi52:.2f}" if hi52 else "—",
             "52-Wk Low",    f"${lo52:.2f}" if lo52 else "—"),
            ("50-Day MA",    _ma_str(ma50) if ma50 else Text("—", style=DIM),
             "200-Day MA",   _ma_str(ma200) if ma200 else Text("—", style=DIM)),
            ("Trailing P/E", f"{trail_pe:.1f}x" if trail_pe else "—",
             "Forward P/E",  f"{fwd_pe:.1f}x" if fwd_pe else "—"),
            ("Beta",         f"{beta:.2f}" if beta else "—",
             "Div Yield",    f"{div_yield*100:.2f}%" if div_yield else "—"),
            ("Short Ratio",  f"{short_ratio:.1f}d" if short_ratio else "—",
             "",             ""),
        ]

        for l1, v1, l2, v2 in rows:
            t.add_row(l1, v1, l2, v2)

        return t

    def _print_stock_info(self, result: dict) -> None:
        console.print(self._get_stock_info_table(result))

    def _get_summary_table(self, result: dict) -> Table:
        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", show_header=True, expand=True)
        t.add_column("Metric", ratio=1)
        t.add_column("Value", justify="right", ratio=1)

        sentiment = result.get("news_sentiment")
        if sentiment is not None:
            if sentiment >= 0.15:
                sentiment_str = f"{sentiment:+.2f}  (bullish)"
            elif sentiment <= -0.15:
                sentiment_str = f"{sentiment:+.2f}  (bearish)"
            else:
                sentiment_str = f"{sentiment:+.2f}  (neutral)"
        else:
            sentiment_str = "n/a"

        rows = [
            ("Score", f"{result.get('score', 0):.1f} / 100"),
            ("Current Price", f"${result.get('current_price', 0):.2f}"),
            ("DCF Implied", f"${result.get('dcf_implied_price', 0):.2f}"),
            ("DCF Upside", f"{result.get('dcf_upside_pct', 0):+.1f}%"),
            ("MC Median (P50)", f"${result.get('mc_p50', 0):.2f}"),
            ("Expected Value", f"${result.get('expected_value', 0):.2f}"),
            ("Implied Hurdle", f"{result.get('implied_hurdle_rate', 0)*100:.2f}%"),
            ("Bull Prob", f"{result.get('bayesian_bull_prob', 0)*100:.1f}%"),
            ("Base Prob", f"{result.get('bayesian_base_prob', 0)*100:.1f}%"),
            ("Bear Prob", f"{result.get('bayesian_bear_prob', 0)*100:.1f}%"),
            ("News Sentiment", sentiment_str),
        ]
        for label, val in rows:
            t.add_row(label, val)

        return t

    def _print_summary_table(self, result: dict) -> None:
        console.print(self._get_summary_table(result))

    def _get_score_breakdown_table(self, result: dict) -> Optional[Group]:
        components = result.get("score_components")
        if not components:
            return None

        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", show_header=True, expand=True)
        t.add_column("Component", ratio=1)
        t.add_column("Score", justify="right", ratio=1)

        labels = {
            "bayesian_upside": "Bayesian Upside  (27%)",
            "mc_upside":       "Monte Carlo P50  (22%)",
            "dcf_upside":      "DCF Upside       (18%)",
            "earnings_momentum": "Earnings Momentum (14%)",
            "sentiment_score": "News Sentiment   (10%)",
            "margin_trend":    "Margin Trend      (9%)",
        }
        for key, label in labels.items():
            val = components.get(key)
            if val is not None:
                t.add_row(label, Text(f"{val:.1f}", style=_score_color(val)))

        return Group(Rule("Score Breakdown", style=GOLD, align="left"), t)

    def _print_score_breakdown(self, result: dict) -> None:
        renderable = self._get_score_breakdown_table(result)
        if renderable:
            console.print(renderable)

    def _get_wacc_derivation_renderable(self, result: dict) -> Optional[Group]:
        w = result.get("wacc_detail", {})
        if not w:
            return None
        terminal_g = result.get("dcf_detail", {}).get("terminal_growth_rate", 0.03)
        
        lines = [Rule("WACC Derivation", style=GOLD, align="left")]
        rf = w.get("rf", 0)
        beta = w.get("beta", 1.0)
        erp = w.get("erp", 0)
        scp = w.get("scp", 0)
        ke = w.get("ke", 0)
        kd = w.get("kd_after_tax", 0)
        we = w.get("we", 1.0)
        wd = w.get("wd", 0.0)
        wacc = w.get("wacc", 0)

        lines.append(Text(f"  Rf  = {rf*100:.2f}%  (10Y UST yield)"))
        lines.append(Text(f"  β   = {beta:.3f}  (yfinance 5-yr monthly regression)"))
        lines.append(Text(f"  ERP = {erp*100:.2f}%  (Damodaran implied ERP)"))
        if scp:
            lines.append(Text(f"  SCP = {scp*100:.2f}%  (Small-cap premium)"))
        lines.append(Text(f"  Ke  = {ke*100:.2f}%  [Rf + β×ERP + SCP]"))
        lines.append(Text(f"  Kd(after-tax) = {kd*100:.2f}%  [{w.get('kd_source', '')}]"))
        lines.append(Text(f"  wE  = {we*100:.1f}%   wD = {wd*100:.1f}%"))
        lines.append(Text(f"  WACC (Our Hurdle)  = {wacc*100:.2f}%", style=f"bold {GOLD}"))
        
        implied_hurdle = result.get("implied_hurdle_rate", 0)
        mos = implied_hurdle - wacc
        mos_style = UP_COLOR if mos > 0 else DOWN_COLOR
        lines.append(Text(f"  Implied Hurdle     = {implied_hurdle*100:.2f}% (Market Pricing)", style=f"bold"))
        lines.append(Text(f"  Spread / MoS       = {mos*100:+.2f}%", style=f"bold {mos_style}"))

        # Add brief interpretation
        interpretation = (
            f"\n  WACC of {wacc*100:.2f}% is the hurdle rate. "
            f"Beta of {beta:.2f} implies {'high' if beta > 1.3 else 'low' if beta < 0.7 else 'average'} volatility."
        )
        lines.append(Text(interpretation, style="dim"))
        
        for note in w.get("notes", []):
            lines.append(Text(f"  Note: {note}", style="yellow"))
            
        return Group(*lines)

    def _print_wacc_derivation(self, result: dict) -> None:
        renderable = self._get_wacc_derivation_renderable(result)
        if renderable:
            console.print(renderable)

    def _get_dcf_table(self, result: dict) -> Optional[Group]:
        dcf = result.get("dcf_detail", {})
        if not dcf:
            return None
        
        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", expand=True)
        t.add_column("Year", justify="right", ratio=1)
        t.add_column("Revenue", justify="right", ratio=2)
        t.add_column("FCF Margin", justify="right", ratio=2)
        t.add_column("FCF", justify="right", ratio=2)
        t.add_column("PV(FCF)", justify="right", ratio=2)

        for row in dcf.get("projection_rows", []):
            t.add_row(
                str(row["year"]),
                f"${row['revenue']/1e9:.2f}B",
                f"{row['fcf_margin']*100:.1f}%",
                f"${row['fcf']/1e9:.2f}B",
                f"${row['pv_fcf']/1e9:.2f}B",
            )

        tv = dcf.get("terminal_value", 0)
        pv_tv = dcf.get("pv_terminal_value", 0)
        ev = dcf.get("enterprise_value", 0)
        implied = dcf.get("implied_price", 0)
        net_debt = dcf.get("net_debt", 0)
        
        info = [
            Text("\nDCF Projection", style="bold"),
            t,
            Text(f"  Terminal Value: ${tv/1e9:.2f}B  →  PV: ${pv_tv/1e9:.2f}B"),
            Text(f"  Enterprise Value: ${ev/1e9:.2f}B  Net Debt: ${net_debt/1e9:.2f}B"),
            Text(f"  DCF Implied Price: ${implied:.2f}", style=f"bold {GOLD}")
        ]

        for note in dcf.get("notes", []):
            color = "yellow" if "WARNING" in note or "Negative" in note else "dim"
            info.append(Text(f"  {note}", style=color))
            
        return Group(*info)

    def _print_dcf_table(self, result: dict) -> None:
        renderable = self._get_dcf_table(result)
        if renderable:
            console.print(renderable)

    def _get_mc_table(self, result: dict) -> Optional[Group]:
        mc = result.get("mc_detail", {})
        if not mc:
            return None
        
        percentiles = mc.get("percentiles", {})
        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", expand=True)
        t.add_column("Percentile", ratio=1)
        t.add_column("Implied Price", justify="right", ratio=1)

        for label in ["P5", "P10", "P25", "P50", "P75", "P90", "P95"]:
            val = percentiles.get(label)
            t.add_row(label, f"${val:.2f}" if val else "—")

        return Group(
            Text(f"\nMonte Carlo ({mc.get('n_sims', '?')} simulations)", style="bold"),
            t,
            Text(f"  P(price > current):  {mc.get('p_above_current', 0)*100:.1f}%"),
            Text(f"  P(price > analyst target): {mc.get('p_above_target', 0)*100:.1f}%")
        )

    def _print_mc_table(self, result: dict) -> None:
        renderable = self._get_mc_table(result)
        if renderable:
            console.print(renderable)

    def _get_bayesian_trace_table(self, result: dict) -> Optional[Group]:
        trace = result.get("bayesian_trace", [])
        if not trace:
            return None
        
        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", expand=True)
        t.add_column("Step", ratio=2)
        t.add_column("Bull", justify="right", ratio=1)
        t.add_column("Base", justify="right", ratio=1)
        t.add_column("Bear", justify="right", ratio=1)

        for step in trace:
            t.add_row(
                step["label"],
                f"{step['bull']*100:.1f}%",
                f"{step['base']*100:.1f}%",
                f"{step['bear']*100:.1f}%",
            )

        return Group(Text("\nBayesian Update Trace", style="bold"), t)

    def _print_bayesian_trace(self, result: dict) -> None:
        renderable = self._get_bayesian_trace_table(result)
        if renderable:
            console.print(renderable)

    def _get_sensitivity_table(self, result: dict) -> Optional[Group]:
        sens = result.get("sensitivity_detail", {})
        if not sens:
            return None
        delta = sens.get("delta_pct", 5)
        
        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", expand=True)
        t.add_column("Scenario", ratio=1)
        t.add_column("Implied Price", justify="right", ratio=1)
        t.add_column("vs Current", justify="right", ratio=1)

        for label, price_key, upside_key in [
            ("Bear",  "bear_price",  "bear_upside_pct"),
            ("Base",  "base_price",  "base_upside_pct"),
            ("Bull",  "bull_price",  "bull_upside_pct"),
        ]:
            price = sens.get(price_key, 0) or 0
            vs_cur = sens.get(upside_key, 0) or 0
            t.add_row(label, f"${price:.2f}", _upside_str(vs_cur))

        duration = sens.get("dV_dwacc_pct", 0)
        duration_info = Text.from_markup(
            f"\n  [bold]Interest Rate Duration:[/bold] {duration:.1f}% "
            f"[dim](Value change per 100bps move in rates)[/dim]"
        )

        return Group(Text(f"\nRevenue Sensitivity (±{delta}%)", style="bold"), t, duration_info)

    def _print_sensitivity_table(self, result: dict) -> None:
        renderable = self._get_sensitivity_table(result)
        if renderable:
            console.print(renderable)

    def _get_catalyst_renderable(self, result: dict) -> Optional[Group]:
        cat = result.get("catalyst_detail") or {}
        cscore = result.get("catalyst_score")
        if cscore is None:
            return None

        archetype = result.get("trade_archetype", "")
        header = Text.from_markup(
            f"\n[bold]Catalyst Score[/bold]  "
            f"[{_score_color(cscore)}]{cscore:.1f} / 100[/{_score_color(cscore)}]  "
            f"[dim]{archetype}[/dim]"
        )

        elements = [header]
        components = cat.get("components", {})
        if components:
            ct = Table(box=box.SQUARE, header_style=f"bold {GOLD}", show_header=True, pad_edge=False, expand=True)
            ct.add_column("Component", ratio=1)
            ct.add_column("Score", justify="right", ratio=1)

            labels = {
                "earnings_acceleration": "Earnings Acceleration  (30%)",
                "smart_money":           "Smart Money            (25%)",
                "analyst_revisions":     "Analyst Revisions      (20%)",
                "price_momentum":        "Price Momentum         (15%)",
                "short_interest":        "Short Interest Setup   (10%)",
            }
            for key, label in labels.items():
                v = components.get(key)
                if v is not None:
                    ct.add_row(label, Text(f"{v:.1f}", style=_score_color(v)))
            elements.append(ct)

        # Data points
        dte = cat.get("days_to_earnings")
        ned = cat.get("next_earnings_date")
        streak = cat.get("earnings_beat_streak")
        insider_buys = cat.get("insider_buys_60d")
        
        info_line = []
        if ned: info_line.append(f"Next report {ned} ({dte}d)")
        if streak: info_line.append(f"Beat streak: {streak}Q")
        if info_line:
            elements.append(Text(" · ".join(info_line), style="dim"))

        for note in cat.get("notes", []):
            elements.append(Text(f"▸ {note}", style=GOLD))

        return Group(*elements)

    def _print_catalyst_section(self, result: dict) -> None:
        renderable = self._get_catalyst_renderable(result)
        if renderable:
            console.print(renderable)

    def _get_options_renderable(self, result: dict) -> Optional[Group]:
        opts = result.get("options_analysis")
        if not opts:
            return None

        exp     = opts.get("expiration", "")
        dte     = opts.get("dte", 0)
        atm_iv  = opts.get("atm_iv", 0)
        regime  = opts.get("iv_regime", "normal")
        call    = opts.get("atm_call") or {}
        put     = opts.get("atm_put") or {}
        strat   = opts.get("strategy") or {}

        regime_color = {"high": "red", "low": "green", "normal": GOLD}.get(regime, GOLD)

        header = Text.from_markup(
            f"\n[bold]Options Analysis[/bold] [dim]{exp} · {dte} DTE[/dim]\n"
            f"  ATM IV: [{regime_color}]{atm_iv*100:.1f}% ({regime} IV)[/{regime_color}]"
        )

        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", pad_edge=False, expand=True)
        t.add_column("",       style=f"bold {GOLD}", ratio=1)
        t.add_column("Strike", justify="right", ratio=1)
        t.add_column("Bid",    justify="right", ratio=1)
        t.add_column("Ask",    justify="right", ratio=1)
        t.add_column("Delta",  justify="right", ratio=1)
        t.add_column("OI",     justify="right", style=DIM, ratio=1)

        for label, c in [("CALL", call), ("PUT", put)]:
            if not c: continue
            greeks = c.get("greeks") or {}
            delta = greeks.get("delta")
            t.add_row(
                label,
                f"${c.get('strike', 0):.2f}",
                f"${c.get('bid', 0):.2f}",
                f"${c.get('ask', 0):.2f}",
                f"{delta:+.3f}" if delta is not None else "—",
                f"{c.get('open_interest', 0):,}",
            )

        elements = [header, t]

        warning = opts.get("earnings_warning")
        if warning:
            elements.append(Text(f"  ⚠  {warning}", style="bold yellow"))
            move = opts.get("implied_move_pct")
            if move is not None:
                elements.append(Text(
                    f"  Compare ±{move:.1f}% implied move to your Bayesian/Catalyst upside — "
                    "if model predicts a larger move, buy premium; if smaller, sell.",
                    style="dim"
                ))

        if strat:
            elements.append(Text(f"  Suggested: {strat.get('name', '')}", style=f"bold {GOLD}"))
            elements.append(Text(f"  {strat.get('rationale', '')}", style="dim"))
            
            summary = strat.get("plain_english_summary")
            if summary:
                elements.append(Text(f"\n  Summary: {summary}", style=f"italic {GOLD}"))

        return Group(*elements)

    def _print_options_section(self, result: dict) -> None:
        renderable = self._get_options_renderable(result)
        if renderable:
            console.print(renderable)

    def get_comparison_table(self, results: list[dict]) -> Table:
        metrics = [
            ("Score", "score", "{:.1f}"),
            ("Price", "current_price", "${:.2f}"),
            ("DCF Implied", "dcf_implied_price", "${:.2f}"),
            ("DCF Upside", "dcf_upside_pct", "{:+.1f}%"),
            ("MC P50", "mc_p50", "${:.2f}"),
            ("E[V]", "expected_value", "${:.2f}"),
            ("Bull Prob", "bayesian_bull_prob", "{:.0%}"),
            ("WACC", "wacc", "{:.2%}"),
        ]

        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", expand=True)
        t.add_column("Metric", ratio=2)
        for r in results:
            ticker = r.get("ticker", "?")
            name = r.get("company_name")
            col_header = f"{ticker}\n{name[:20]}" if name else ticker
            t.add_column(col_header, justify="right", ratio=3)

        for label, key, fmt in metrics:
            row = [label]
            for r in results:
                val = r.get(key)
                row.append(fmt.format(val) if val is not None else "—")
            t.add_row(*row)
        return t

    def print_comparison(self, results: list[dict]) -> None:
        if not results:
            console.print("[red]No results to compare.[/red]")
            return
        console.print(self.get_comparison_table(results))

    def get_help_renderables(self, config: dict) -> Group:
        """Returns a comprehensive Group of help renderables for TUI or CLI."""
        elements = []

        # ── Commands ──────────────────────────────────────────────────────────
        elements.append(Rule("COMMANDS", style=GOLD, align="left"))
        cmd_table = Table(box=box.SQUARE, header_style=f"bold {GOLD}", show_header=True, pad_edge=False, expand=True)
        cmd_table.add_column("Command",  style=f"bold {GOLD}", ratio=1)
        cmd_table.add_column("Usage",    style="white",        ratio=2)
        cmd_table.add_column("Description", ratio=4)

        cmd_table.add_row("init", "sanctum init", "Initialize database and default configuration.")
        cmd_table.add_row("watchlist", "sanctum watchlist [add|remove|list]", "Manage your persistent ticker watchlist.")
        cmd_table.add_row("portfolio", "sanctum portfolio [add|remove|show]", "Track holdings and get rebalancing suggestions.")
        cmd_table.add_row("screen", "sanctum screen [--tickers A,B]", "Scan a universe and rank by composite score.")
        cmd_table.add_row("analyze", "sanctum analyze TICKER", "Deep-dive: DCF, Monte Carlo, Bayesian, and Sensitivity.")
        cmd_table.add_row("compare", "sanctum compare T1 T2", "Side-by-side comparison of multiple tickers.")
        elements.append(cmd_table)

        # ── Universe sources ──────────────────────────────────────────────────
        elements.append(Rule("UNIVERSE SOURCES", style=GOLD, align="left"))
        elements.append(Text("Define the scope of 'sanctum screen'.", style=DIM))
        src_table = Table(box=box.SQUARE, header_style=f"bold {GOLD}", show_header=False, pad_edge=False, expand=True)
        src_table.add_column("Source", style=f"bold white", ratio=1)
        src_table.add_column("Description", ratio=4)
        src_table.add_row("all_us",    "Full US market (~8,000 tickers via ticker-library)")
        src_table.add_row("sp500",     "S&P 500 constituents (live from Wikipedia)")
        src_table.add_row("nasdaq100", "Nasdaq-100 constituents")
        src_table.add_row("watchlist", "Your persistent watchlist (managed via 'watchlist' command)")
        src_table.add_row("custom",    "Static list defined in config.yaml")
        elements.append(src_table)

        # ── Pipeline ──────────────────────────────────────────────────────────
        elements.append(Rule("ANALYSIS PIPELINE", style=GOLD, align="left"))
        pipe_table = Table(box=box.SQUARE, show_header=False, pad_edge=False, expand=True)
        pipe_table.add_column("Step", style=f"bold {GOLD}", ratio=1)
        pipe_table.add_column("Description", ratio=4)
        pipe_table.add_row("1. WACC", "CAPM-based hurdle rate with iterative cost of debt estimation.")
        pipe_table.add_row("2. DCF", "7-year unlevered FCF projection with multi-stage growth blending.")
        pipe_table.add_row("3. Monte Carlo", "10,000 log-normal simulations for revenue/margin volatility.")
        pipe_table.add_row("4. Bayesian", "Probabilistic update across 5 conviction factors.")
        pipe_table.add_row("5. Sensitivity", f"DCF stress-test at ±{config.get('sensitivity', {}).get('revenue_delta_pct', 5)}% revenue delta.")
        pipe_table.add_row("6. Scoring", "Weighted composite: Bayesian (30%), MC (25%), DCF (20%), etc.")
        elements.append(pipe_table)

        # ── Config overrides ──────────────────────────────────────────────────
        elements.append(Rule("CLI OVERRIDES", style=GOLD, align="left"))
        elements.append(Text("Use '--set key.path=value' to override config.yaml temporarily.", style=DIM))
        ex_table = Table(box=box.SQUARE, show_header=False, pad_edge=False, expand=True)
        ex_table.add_column("Example", style="white", no_wrap=True, ratio=1)
        ex_table.add_column("Effect", style=DIM, ratio=1)
        ex_table.add_row("  --set montecarlo.n_simulations=5000", "Faster, less precise simulations")
        ex_table.add_row("  --set dcf.terminal_growth_rate=0.02", "Conservative terminal assumption")
        ex_table.add_row("  --set wacc.risk_free_rate=0.045", "Manual 10Y Treasury yield override")
        elements.append(ex_table)

        # ── Current config snapshot ───────────────────────────────────────────
        elements.append(Rule("ACTIVE CONFIGURATION", style=GOLD, align="left"))
        snap_table = Table(box=box.SQUARE, show_header=False, pad_edge=False, expand=True)
        snap_table.add_column("Parameter", style="white", no_wrap=True, ratio=1)
        snap_table.add_column("Value", style=f"{GOLD}", ratio=1)

        wacc_cfg = config.get("wacc", {})
        mc_cfg   = config.get("montecarlo", {})
        dcf_cfg  = config.get("dcf", {})
        
        snap_table.add_row("Universe Source", str(config.get("universe", {}).get("source", "all_us")))
        snap_table.add_row("Risk-free Rate", f"{wacc_cfg.get('risk_free_rate', 0.043):.2%}")
        snap_table.add_row("Equity Risk Premium", f"{wacc_cfg.get('equity_risk_premium', 0.055):.2%}")
        snap_table.add_row("Terminal Growth", f"{dcf_cfg.get('terminal_growth_rate', 0.03):.2%}")
        snap_table.add_row("MC Sims (Analyze)", str(mc_cfg.get("n_simulations", 10000)))
        elements.append(snap_table)
        elements.append(Text.from_markup("\n[dim]Press any navigation key to exit help.[/dim]\n"))

        return Group(*elements)

    def print_help(self, config: dict) -> None:
        self.print_header()
        console.print(self.get_help_renderables(config))


    def get_portfolio_renderable(self, holdings: dict, scored: list[dict], suggestions: list[dict]) -> Group:
        t = Table(box=box.SQUARE, header_style=f"bold {GOLD}", expand=True)
        t.add_column("Ticker", ratio=1)
        t.add_column("Shares", justify="right", ratio=1)
        t.add_column("Score", justify="right", ratio=1)
        t.add_column("Upside", justify="right", ratio=1)

        scored_map = {r["ticker"]: r for r in scored}
        for ticker, shares in holdings.items():
            r = scored_map.get(ticker, {})
            t.add_row(
                ticker,
                str(shares),
                f"{r.get('score', 0):.1f}" if r else "—",
                f"{r.get('dcf_upside_pct', 0):+.1f}%" if r else "—",
            )
        
        elements = [Text("\nCurrent Portfolio", style=f"bold {GOLD}"), t]

        if suggestions:
            elements.append(Text("\nRebalancing Suggestions", style="bold"))
            for s in suggestions:
                elements.append(Text(f"  {s['action']:8s} {s['ticker']}  {s.get('reason', '')}"))
        
        return Group(*elements)

    def print_portfolio(self, holdings: dict, scored: list[dict], suggestions: list[dict]) -> None:
        console.print(self.get_portfolio_renderable(holdings, scored, suggestions))

    def get_analysis_tui(self, result: dict) -> Group:
        """
        Compact analysis view for TUI display — fits in a fixed-height panel.
        The full math breakdown is available via: sanctum analyze <ticker>
        """
        ticker    = result.get("ticker", "?")
        name      = result.get("company_name", "")
        score     = result.get("score", 0.0)
        cscore    = result.get("catalyst_score", 50.0)
        archetype = result.get("trade_archetype", "")

        header = Text.from_markup(
            f"[bold {GOLD}]{ticker}[/bold {GOLD}]"
            + (f"  [dim]{name}[/dim]" if name else "")
        )
        arch_text = Text(f"  {archetype}", style="dim") if archetype else None

        # Left column: key valuation metrics
        left = Table(box=box.SQUARE, show_header=False, pad_edge=False, expand=True)
        left.add_column("Label", style=DIM, no_wrap=True, ratio=1)
        left.add_column("Value", no_wrap=True, ratio=1)

        price      = result.get("current_price")
        dcf_price  = result.get("dcf_implied_price")
        dcf_up     = result.get("dcf_upside_pct")
        mc_p50     = result.get("mc_p50")
        ev         = result.get("expected_value")
        bull       = result.get("bayesian_bull_prob")
        base       = result.get("bayesian_base_prob")
        bear       = result.get("bayesian_bear_prob")
        wacc_val   = result.get("wacc")
        implied_h  = result.get("implied_hurdle_rate")
        duration   = result.get("sensitivity_detail", {}).get("dV_dwacc_pct")

        left.add_row("Fund. Score", Text(f"{score:.1f}", style=_score_color(score)))
        left.add_row("Catalyst",    Text(f"{cscore:.1f}", style=_score_color(cscore)))
        left.add_row("Price",       f"${price:.2f}" if price else "—")
        left.add_row(
            "DCF Implied",
            f"${dcf_price:.2f}  ({dcf_up:+.1f}%)" if dcf_price and dcf_up is not None else "—"
        )
        left.add_row("MC P50",  f"${mc_p50:.2f}" if mc_p50 else "—")
        left.add_row("E[V]",    f"${ev:.2f}"    if ev     else "—")
        left.add_row(
            "Bull/Base/Bear",
            f"{bull*100:.0f}% / {base*100:.0f}% / {bear*100:.0f}%"
            if bull is not None else "—"
        )
        left.add_row("WACC / Implied", f"{wacc_val*100:.1f}% / {implied_h*100:.1f}%" if wacc_val and implied_h else "—")
        left.add_row("Duration (Rate)", f"{duration:.1f}% / 100bps" if duration is not None else "—")

        # Right column: score component breakdown
        right = Table(box=box.SQUARE, show_header=False, pad_edge=False, expand=True)
        right.add_column("Component", style=DIM, no_wrap=True, ratio=1)
        right.add_column("Score", justify="right", no_wrap=True, ratio=1)

        components = result.get("score_components", {})
        for key, label in [
            ("bayesian_upside",  "Bayesian"),
            ("mc_upside",        "Monte Carlo"),
            ("dcf_upside",       "DCF"),
            ("earnings_momentum","Earnings Mom."),
            ("sentiment_score",  "Sentiment"),
            ("margin_trend",     "Margin Trend"),
        ]:
            v = components.get(key)
            if v is not None:
                right.add_row(label, Text(f"{v:.0f}", style=_score_color(v)))

        columns = Columns([left, right], expand=True)

        elements: list = [header]
        if arch_text:
            elements.append(arch_text)
        elements.append(columns)

        # Surface important DCF notes (warnings only)
        dcf_detail = result.get("dcf_detail") or {}
        for note in (dcf_detail.get("notes") or []):
            if "WARNING" in note or "Negative" in note:
                elements.append(Text(f"  ⚠  {note[:90]}", style="yellow"))

        # Options summary line
        opts = result.get("options_analysis")
        if opts:
            regime = opts.get("iv_regime", "normal")
            regime_color = {"high": "red", "low": "green", "normal": GOLD}.get(regime, GOLD)
            atm_iv = opts.get("atm_iv", 0)
            dte    = opts.get("dte", 0)
            strat  = opts.get("strategy") or {}
            line   = (
                f"  Options: [{regime_color}]IV {atm_iv*100:.0f}% ({regime})[/{regime_color}]"
                f"  {dte}DTE"
            )
            if strat.get("name"):
                line += f"  → {strat['name']}"
            elements.append(Text.from_markup(line))
            warning = opts.get("earnings_warning")
            if warning:
                elements.append(Text(f"  ⚠  {warning}", style="bold yellow"))

        # Model errors (brief)
        errors = result.get("errors") or []
        for err in errors[:2]:
            elements.append(Text(f"  ✗ {err}", style="dim red"))

        elements.append(Text.from_markup(
            f"\n  [dim]Full math: sanctum analyze {ticker}[/dim]"
        ))
        return Group(*elements)
