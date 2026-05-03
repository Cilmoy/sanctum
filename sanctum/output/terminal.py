"""
terminal.py — Rich terminal output for Sanctum.

Dark theme, gold accent. Tables should be clean and scannable.
"""

import logging
from typing import Any, List, Dict
from contextlib import contextmanager

from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text
from rich.panel import Panel
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

    def print_header(self, subtitle: str = "") -> None:
        title = f"[bold {GOLD}]{self.brand}[/bold {GOLD}]"
        if subtitle:
            title += f"\n[dim]{subtitle}[/dim]"
        console.print(Panel(title, border_style=GOLD, expand=False))

    def print_screen_results(self, results: list[dict], shortlisted: list[dict]) -> None:
        top_n = self.config.get("output", {}).get("top_n", 20)
        display = results[:top_n]

        console.print(
            f"\n[{GOLD}]Universe:[/{GOLD}] {len(results)} scored  "
            f"[{GOLD}]Shortlisted:[/{GOLD}] {len(shortlisted)}\n"
        )

        table = Table(
            box=box.SIMPLE_HEAD,
            header_style=f"bold {GOLD}",
            show_lines=False,
            pad_edge=False,
        )
        table.add_column("Rank", justify="right", style=DIM)
        table.add_column("Ticker", style=f"bold {GOLD}", no_wrap=True)
        table.add_column("Name", style=DIM)
        table.add_column("F.Score", justify="right")
        table.add_column("C.Score", justify="right")
        table.add_column("Archetype", style=DIM)
        table.add_column("Price", justify="right")
        table.add_column("Upside", justify="right")
        table.add_column("Sector", style=DIM)

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
                name[:24],
                Text(f"{score:.1f}", style=_score_color(score)),
                Text(f"{cscore:.1f}", style=_score_color(cscore)),
                arch_short[:22],
                f"${price:.2f}" if price else "—",
                _upside_str(upside),
                sector[:12] if sector else "—",
            )

        console.print(table)

    def print_analysis(self, result: dict, show_math: bool = True) -> None:
        ticker = result.get("ticker", "?")
        name = result.get("company_name")
        header = f"── {ticker}" + (f"  {name}" if name else "") + " ──────────────────────────────────"
        console.print(f"\n[bold {GOLD}]{header}[/bold {GOLD}]")

        self._print_stock_info(result)
        self._print_summary_table(result)
        self._print_score_breakdown(result)
        self._print_catalyst_section(result)

        if show_math:
            self._print_wacc_derivation(result)
            self._print_dcf_table(result)
            self._print_mc_table(result)
            self._print_bayesian_trace(result)
            self._print_sensitivity_table(result)

        self._print_options_section(result)

    def _print_stock_info(self, result: dict) -> None:
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

        t = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        t.add_column("Label", style=DIM, no_wrap=True)
        t.add_column("Value", no_wrap=True)
        t.add_column("Label2", style=DIM, no_wrap=True)
        t.add_column("Value2", no_wrap=True)

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

        console.print(t)

    def _print_summary_table(self, result: dict) -> None:
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}", show_header=True)
        t.add_column("Metric")
        t.add_column("Value", justify="right")

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
            ("Bull Prob", f"{result.get('bayesian_bull_prob', 0)*100:.1f}%"),
            ("Base Prob", f"{result.get('bayesian_base_prob', 0)*100:.1f}%"),
            ("Bear Prob", f"{result.get('bayesian_bear_prob', 0)*100:.1f}%"),
            ("News Sentiment", sentiment_str),
        ]
        for label, val in rows:
            t.add_row(label, val)

        console.print(t)

    def _print_score_breakdown(self, result: dict) -> None:
        components = result.get("score_components")
        if not components:
            return

        console.print(f"\n[bold]Score Breakdown[/bold]")
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}", show_header=True)
        t.add_column("Component")
        t.add_column("Score", justify="right")

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

        console.print(t)

    def _print_wacc_derivation(self, result: dict) -> None:
        w = result.get("wacc_detail", {})
        if not w:
            return
        terminal_g = result.get("dcf_detail", {}).get("terminal_growth_rate", 0.03)
        console.print(f"\n[bold]WACC Derivation[/bold]")
        rf = w.get("rf", 0)
        beta = w.get("beta", 1.0)
        erp = w.get("erp", 0)
        scp = w.get("scp", 0)
        ke = w.get("ke", 0)
        kd = w.get("kd_after_tax", 0)
        we = w.get("we", 1.0)
        wd = w.get("wd", 0.0)
        wacc = w.get("wacc", 0)

        console.print(f"  Rf  = {rf*100:.2f}%  [dim](10Y UST yield — set in config, not fetched live)[/dim]")
        console.print(f"  β   = {beta:.3f}  [dim](yfinance 5-yr monthly regression vs S&P 500)[/dim]")
        console.print(f"  ERP = {erp*100:.2f}%  [dim](Damodaran implied equity risk premium — config constant)[/dim]")
        if scp:
            console.print(f"  SCP = {scp*100:.2f}%  [dim](Duff & Phelps small-cap premium — market cap < threshold)[/dim]")
        console.print(f"  Ke  = {ke*100:.2f}%  [dim][Rf + β×ERP + SCP][/dim]")
        console.print(f"  Kd(after-tax) = {kd*100:.2f}%  [dim][{w.get('kd_source', '')}][/dim]")
        console.print(f"  wE  = {we*100:.1f}%   wD = {wd*100:.1f}%")
        console.print(f"  [bold {GOLD}]WACC = {wacc*100:.2f}%[/bold {GOLD}]")

        # Plain-English interpretation — each print is self-contained
        console.print(f"")
        console.print(f"  [dim]WACC = {wacc*100:.2f}% is the hurdle rate: the minimum annualized return the[/dim]")
        console.print(f"  [dim]business must earn to justify its cost of capital. The DCF discounts[/dim]")
        console.print(f"  [dim]every projected cash flow at this rate — a higher WACC means a lower[/dim]")
        console.print(f"  [dim]implied price, all else equal.[/dim]")

        if beta > 1.3:
            console.print(f"  [dim]β = {beta:.2f} is elevated: this stock historically moves {beta:.1f}x the market,[/dim]")
            console.print(f"  [dim]which drives Ke to {ke*100:.1f}% and pushes WACC up.[/dim]")
        elif beta < 0.7:
            console.print(f"  [dim]β = {beta:.2f} is low — the stock moves less than the market,[/dim]")
            console.print(f"  [dim]which suppresses Ke to {ke*100:.1f}% and holds WACC down.[/dim]")
        else:
            console.print(f"  [dim]β = {beta:.2f} is broadly market-correlated, giving Ke = {ke*100:.1f}%.[/dim]")

        if wd > 0.15:
            console.print(f"  [dim]Debt is {wd*100:.0f}% of capital. Kd ({kd*100:.1f}% after-tax) < Ke ({ke*100:.1f}%),[/dim]")
            console.print(f"  [dim]so the leverage pulls the blended WACC below the all-equity cost.[/dim]")
        elif wd < 0.05:
            console.print(f"  [dim]Near-zero debt ({wd*100:.0f}%) — WACC ≈ Ke, entirely equity-funded.[/dim]")

        console.print(f"  [dim]Key assumptions underpinning everything below:[/dim]")
        console.print(f"  [dim]  • Terminal growth rate ({terminal_g*100:.1f}%): set in config — the DCF is[/dim]")
        console.print(f"  [dim]    highly sensitive to this. A 0.5pp change can move implied price 10–20%.[/dim]")
        console.print(f"  [dim]  • FCF margin: trailing 3-yr average (or sector fallback if negative/sparse).[/dim]")
        console.print(f"  [dim]    Projects past margins into the future — verify against guidance.[/dim]")
        console.print(f"  [dim]  • ERP ({erp*100:.1f}%) and Rf ({rf*100:.2f}%) are config constants, not live.[/dim]")
        console.print(f"  [dim]    Update wacc.equity_risk_premium and wacc.risk_free_rate when rates shift.[/dim]")

        for note in w.get("notes", []):
            console.print(f"  [yellow]Note: {note}[/yellow]")

    def _print_dcf_table(self, result: dict) -> None:
        dcf = result.get("dcf_detail", {})
        if not dcf:
            return
        console.print(f"\n[bold]DCF Projection[/bold]")
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}")
        t.add_column("Year", justify="right")
        t.add_column("Revenue", justify="right")
        t.add_column("FCF Margin", justify="right")
        t.add_column("FCF", justify="right")
        t.add_column("PV(FCF)", justify="right")

        for row in dcf.get("projection_rows", []):
            t.add_row(
                str(row["year"]),
                f"${row['revenue']/1e9:.2f}B",
                f"{row['fcf_margin']*100:.1f}%",
                f"${row['fcf']/1e9:.2f}B",
                f"${row['pv_fcf']/1e9:.2f}B",
            )

        console.print(t)
        tv = dcf.get("terminal_value", 0)
        pv_tv = dcf.get("pv_terminal_value", 0)
        ev = dcf.get("enterprise_value", 0)
        implied = dcf.get("implied_price", 0)
        net_debt = dcf.get("net_debt", 0)
        console.print(f"  Terminal Value: ${tv/1e9:.2f}B  →  PV: ${pv_tv/1e9:.2f}B")
        console.print(f"  Enterprise Value: ${ev/1e9:.2f}B  Net Debt: ${net_debt/1e9:.2f}B")
        console.print(f"  [bold {GOLD}]DCF Implied Price: ${implied:.2f}[/bold {GOLD}]")

        for note in dcf.get("notes", []):
            color = "yellow" if "WARNING" in note or "Negative" in note else "dim"
            console.print(f"  [{color}]  {note}[/{color}]")

    def _print_mc_table(self, result: dict) -> None:
        mc = result.get("mc_detail", {})
        if not mc:
            return
        console.print(f"\n[bold]Monte Carlo ({mc.get('n_sims', '?')} simulations)[/bold]")
        percentiles = mc.get("percentiles", {})
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}")
        t.add_column("Percentile")
        t.add_column("Implied Price", justify="right")

        for label in ["P5", "P10", "P25", "P50", "P75", "P90", "P95"]:
            val = percentiles.get(label)
            t.add_row(label, f"${val:.2f}" if val else "—")

        console.print(t)
        console.print(f"  P(price > current):  {mc.get('p_above_current', 0)*100:.1f}%")
        console.print(f"  P(price > analyst target): {mc.get('p_above_target', 0)*100:.1f}%")

    def _print_bayesian_trace(self, result: dict) -> None:
        trace = result.get("bayesian_trace", [])
        if not trace:
            return
        console.print(f"\n[bold]Bayesian Update Trace[/bold]")
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}")
        t.add_column("Step")
        t.add_column("Bull", justify="right")
        t.add_column("Base", justify="right")
        t.add_column("Bear", justify="right")

        for step in trace:
            t.add_row(
                step["label"],
                f"{step['bull']*100:.1f}%",
                f"{step['base']*100:.1f}%",
                f"{step['bear']*100:.1f}%",
            )

        console.print(t)

    def _print_sensitivity_table(self, result: dict) -> None:
        sens = result.get("sensitivity_detail", {})
        if not sens:
            return
        delta = sens.get("delta_pct", 5)
        console.print(f"\n[bold]Revenue Sensitivity (±{delta}%)[/bold]")
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}")
        t.add_column("Scenario")
        t.add_column("Implied Price", justify="right")
        t.add_column("vs Current", justify="right")

        for label, price_key, upside_key in [
            ("Bear",  "bear_price",  "bear_upside_pct"),
            ("Base",  "base_price",  "base_upside_pct"),
            ("Bull",  "bull_price",  "bull_upside_pct"),
        ]:
            price = sens.get(price_key, 0) or 0
            vs_cur = sens.get(upside_key, 0) or 0
            t.add_row(label, f"${price:.2f}", _upside_str(vs_cur))

        console.print(t)

    def _print_catalyst_section(self, result: dict) -> None:
        cat = result.get("catalyst_detail") or {}
        cscore = result.get("catalyst_score")
        if cscore is None:
            return

        archetype = result.get("trade_archetype", "")
        console.print(f"\n[bold]Catalyst Score[/bold]  "
                      f"[{_score_color(cscore)}]{cscore:.1f} / 100[/{_score_color(cscore)}]  "
                      f"[dim]{archetype}[/dim]")

        # Component breakdown
        components = cat.get("components", {})
        if components:
            ct = Table(box=box.SIMPLE, header_style=f"bold {GOLD}", show_header=True, pad_edge=False)
            ct.add_column("Component")
            ct.add_column("Score", justify="right")

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
            console.print(ct)

        # Key data points
        dte = cat.get("days_to_earnings")
        ned = cat.get("next_earnings_date")
        streak = cat.get("earnings_beat_streak")
        avg_beat = cat.get("earnings_beat_avg_pct")
        accelerating = cat.get("earnings_beat_accelerating")
        insider_buys = cat.get("insider_buys_60d")
        insider_val = cat.get("insider_buy_value_60d")
        insider_own = cat.get("insider_own_pct")
        inst_own = cat.get("institutional_own_pct")
        net_upgrades = cat.get("analyst_net_upgrades_30d")
        short_pct = cat.get("short_pct_float")

        console.print(f"\n  [dim]Earnings:[/dim]", end="")
        if ned:
            console.print(f"  [dim]Next report {ned}" + (f" ({dte}d)" if dte is not None else "") + "[/dim]", end="")
        if streak is not None:
            console.print(f"  [dim]·  Beat streak: {streak}Q[/dim]", end="")
        if avg_beat is not None:
            console.print(f"  [dim]·  Avg beat: {avg_beat*100:+.1f}%[/dim]", end="")
        if accelerating is not None:
            acc_str = "accelerating ↑" if accelerating else "decelerating ↓"
            console.print(f"  [dim]·  {acc_str}[/dim]", end="")
        console.print("")

        console.print(f"  [dim]Ownership:[/dim]", end="")
        if insider_own is not None:
            console.print(f"  [dim]Insiders {insider_own*100:.1f}%[/dim]", end="")
        if inst_own is not None:
            console.print(f"  [dim]·  Institutions {inst_own*100:.1f}%[/dim]", end="")
        if insider_buys is not None:
            val_str = f"  (${insider_val/1e6:.1f}M)" if insider_val else ""
            console.print(f"  [dim]·  {insider_buys} insider buy(s) last 60d{val_str}[/dim]", end="")
        console.print("")

        console.print(f"  [dim]Analysts:[/dim]", end="")
        if net_upgrades is not None:
            sign = "+" if net_upgrades >= 0 else ""
            console.print(f"  [dim]Net {sign}{net_upgrades} upgrades/downgrades (30d)[/dim]", end="")
        if short_pct is not None:
            console.print(f"  [dim]·  Short float: {short_pct*100:.1f}%[/dim]", end="")
        console.print("")

        # Highlight notable signals from notes
        for note in cat.get("notes", []):
            console.print(f"  [{GOLD}]▸ {note}[/{GOLD}]")

    def _print_options_section(self, result: dict) -> None:
        opts = result.get("options_analysis")
        if not opts:
            return

        exp     = opts.get("expiration", "")
        dte     = opts.get("dte", 0)
        atm_iv  = opts.get("atm_iv", 0)
        hv30    = opts.get("hv30")
        regime  = opts.get("iv_regime", "normal")
        call    = opts.get("atm_call") or {}
        put     = opts.get("atm_put") or {}
        strat   = opts.get("strategy") or {}

        regime_color = {"high": "red", "low": "green", "normal": GOLD}.get(regime, GOLD)

        console.print(f"\n[bold]Options Analysis[/bold]  "
                      f"[dim]{exp}  ·  {dte} DTE[/dim]")

        # IV summary line
        hv_str = f"  HV30 {hv30*100:.1f}%" if hv30 else ""
        console.print(
            f"  ATM IV: [{regime_color}]{atm_iv*100:.1f}%  ({regime} IV)[/{regime_color}]"
            f"[dim]{hv_str}[/dim]"
        )

        # ATM contracts table
        t = Table(box=box.SIMPLE, header_style=f"bold {GOLD}", pad_edge=False)
        t.add_column("",       style=f"bold {GOLD}", no_wrap=True)
        t.add_column("Strike", justify="right")
        t.add_column("Bid",    justify="right")
        t.add_column("Ask",    justify="right")
        t.add_column("IV",     justify="right")
        t.add_column("Delta",  justify="right")
        t.add_column("Gamma",  justify="right")
        t.add_column("Theta",  justify="right")
        t.add_column("Vega",   justify="right")
        t.add_column("Vol",    justify="right", style=DIM)
        t.add_column("OI",     justify="right", style=DIM)

        def _g(contract, key, fmt):
            greeks = contract.get("greeks") or {}
            v = greeks.get(key)
            return fmt.format(v) if v is not None else "—"

        for label, c in [("CALL", call), ("PUT", put)]:
            if not c:
                continue
            t.add_row(
                label,
                f"${c.get('strike', 0):.2f}",
                f"${c.get('bid', 0):.2f}",
                f"${c.get('ask', 0):.2f}",
                f"{c.get('iv', 0)*100:.1f}%",
                _g(c, "delta", "{:+.3f}"),
                _g(c, "gamma", "{:.4f}"),
                _g(c, "theta", "{:+.3f}"),
                _g(c, "vega",  "{:.3f}"),
                f"{c.get('volume', 0):,}",
                f"{c.get('open_interest', 0):,}",
            )
        console.print(t)

        # Strategy recommendation
        if strat:
            console.print(f"  [bold {GOLD}]Suggested: {strat.get('name', '')}[/bold {GOLD}]")
            console.print(f"  [dim]{strat.get('rationale', '')}[/dim]")

            legs = strat.get("legs", [])
            if legs:
                console.print("")
                lt = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
                lt.add_column("Action", style="bold")
                lt.add_column("Type")
                lt.add_column("Strike", justify="right")
                lt.add_column("Expiry", style=DIM)
                lt.add_column("Note", style=DIM)
                for leg in legs:
                    action_color = "green" if leg.get("action") == "BUY" else "red"
                    lt.add_row(
                        f"[{action_color}]{leg.get('action', '')}[/{action_color}]",
                        leg.get("type", ""),
                        f"${leg.get('strike', 0):.2f}",
                        leg.get("expiry", ""),
                        leg.get("note", ""),
                    )
                console.print(lt)

            note = strat.get("execution_note", "")
            if note:
                console.print(f"  [dim]{note}[/dim]")

    def print_comparison(self, results: list[dict]) -> None:
        if not results:
            console.print("[red]No results to compare.[/red]")
            return

        # Summary row per ticker
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

        t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {GOLD}")
        t.add_column("Metric")
        for r in results:
            ticker = r.get("ticker", "?")
            name = r.get("company_name")
            col_header = f"{ticker}\n{name[:20]}" if name else ticker
            t.add_column(col_header, justify="right")

        for label, key, fmt in metrics:
            row = [label]
            for r in results:
                val = r.get(key)
                row.append(fmt.format(val) if val is not None else "—")
            t.add_row(*row)

        console.print(t)

    def print_help(self, config: dict) -> None:
        from rich.padding import Padding

        self.print_header()

        # ── Commands ──────────────────────────────────────────────────────────
        console.print(f"\n[bold {GOLD}]Commands[/bold {GOLD}]")
        cmd_table = Table(box=box.SIMPLE, header_style=f"bold {GOLD}", show_header=True, pad_edge=False)
        cmd_table.add_column("Command",  style=f"bold {GOLD}", no_wrap=True)
        cmd_table.add_column("Usage",    style="white",        no_wrap=True)
        cmd_table.add_column("Description")

        cmd_table.add_row(
            "init",
            "sanctum init",
            "Interactive setup: welcome message and database initialization.",
        )
        cmd_table.add_row(
            "watchlist",
            "sanctum watchlist [add|remove|list]",
            "Manage your persistent watchlist. (e.g., 'sanctum watchlist add NVDA')",
        )
        cmd_table.add_row(
            "portfolio",
            "sanctum portfolio [add|remove|show|rebalance]",
            "Manage your persistent portfolio. (e.g., 'sanctum portfolio add TSM 50')",
        )
        cmd_table.add_row(
            "screen",
            "sanctum screen [--tickers A,B,C]",
            "Screen a universe. Defaults to watchlist if no tickers provided.",
        )
        cmd_table.add_row(
            "analyze",
            "sanctum analyze TICKER",
            "Deep-dive a single stock: WACC, DCF, MC, Bayesian, and sensitivity.",
        )
        cmd_table.add_row(
            "compare",
            "sanctum compare TICK1 TICK2 ...",
            "Side-by-side comparison of two or more stocks.",
        )
        cmd_table.add_row(
            "help",
            "sanctum help",
            "Show this dashboard.",
        )
        console.print(cmd_table)

        # ── Universe sources ──────────────────────────────────────────────────
        console.print(f"\n[bold {GOLD}]Universe Sources[/bold {GOLD}]  "
                      f"[dim](set via config.yaml → universe.source)[/dim]")
        src_table = Table(box=box.SIMPLE, header_style=f"bold {GOLD}", show_header=False, pad_edge=False)
        src_table.add_column("Source", style=f"bold white", no_wrap=True)
        src_table.add_column("Description")
        src_table.add_row("all_us",    "All NYSE + NASDAQ + AMEX listed common stocks (~8,000 tickers)")
        src_table.add_row("sp500",     "S&P 500 constituents (fetched from Wikipedia)")
        src_table.add_row("nasdaq100", "Nasdaq-100 constituents")
        src_table.add_row("custom",    "Tickers listed in universe.custom_tickers in config.yaml")
        src_table.add_row("watchlist", "Tickers in your persistent watchlist (sanctum watchlist list)")
        console.print(src_table)

        # ── Pipeline ──────────────────────────────────────────────────────────
        console.print(f"\n[bold {GOLD}]Analysis Pipeline[/bold {GOLD}]")
        steps = [
            ("1  WACC",        "CAPM + optional small-cap premium. Cost of debt derived from financials."),
            ("2  DCF",         "7-year unlevered FCF projection. Growth blends historical CAGR + sector median + analyst consensus."),
            ("3  Monte Carlo", "10,000 simulations (log-normal revenue, antithetic variates). Reports P5–P95 + P(upside)."),
            ("4  Bayesian",    "Sequential update on 5 evidence factors: revenue growth, gross margin, forward P/E, analyst upside, EPS surprise."),
            ("5  E[V]",        "Expected value = P(bull)×PT_bull + P(base)×PT_base + P(bear)×PT_bear."),
            ("6  Sensitivity", f"DCF re-run at ±{config.get('sensitivity', {}).get('revenue_delta_pct', 5)}% revenue. Reports dV/dr and asymmetry flag."),
            ("7  Score",       "Weighted composite 0–100: Bayesian 30%, MC 25%, DCF 20%, earnings momentum 15%, margin trend 10%."),
        ]
        pipe_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        pipe_table.add_column("Step", style=f"bold {GOLD}", no_wrap=True)
        pipe_table.add_column("Description")
        for step, desc in steps:
            pipe_table.add_row(step, desc)
        console.print(pipe_table)

        # ── Config overrides ──────────────────────────────────────────────────
        console.print(f"\n[bold {GOLD}]On-the-fly Config Overrides[/bold {GOLD}]  "
                      f"[dim](--set KEY=VALUE, dot-notation)[/dim]")
        ex_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        ex_table.add_column("Example", style="white", no_wrap=True)
        ex_table.add_column("Effect", style=DIM)
        examples = [
            ("--set montecarlo.n_simulations=5000",   "Reduce MC paths for speed"),
            ("--set scoring.shortlist_threshold=70",  "Raise conviction bar"),
            ("--set dcf.terminal_growth_rate=0.025",  "Lower terminal growth assumption"),
            ("--set wacc.equity_risk_premium=0.06",   "Adjust ERP"),
            ("--set output.show_math=false",          "Suppress derivation output"),
            ("--set output.top_n=10",                 "Show only top 10 in screen"),
        ]
        for ex, effect in examples:
            ex_table.add_row(f"  {ex}", effect)
        console.print(ex_table)

        # ── Current config snapshot ───────────────────────────────────────────
        console.print(f"\n[bold {GOLD}]Active Config Snapshot[/bold {GOLD}]")
        snap_table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        snap_table.add_column("Parameter", style="white", no_wrap=True)
        snap_table.add_column("Value", style=f"{GOLD}")

        wacc_cfg = config.get("wacc", {})
        mc_cfg   = config.get("montecarlo", {})
        dcf_cfg  = config.get("dcf", {})
        sc_cfg   = config.get("scoring", {})
        uni_cfg  = config.get("universe", {})

        snap_table.add_row("Universe source",         str(uni_cfg.get("source", "all_us")))
        snap_table.add_row("Min market cap",          f"${uni_cfg.get('min_market_cap_B', 2)}B")
        snap_table.add_row("Risk-free rate",          f"{wacc_cfg.get('risk_free_rate', 0.043):.1%}")
        snap_table.add_row("Equity risk premium",     f"{wacc_cfg.get('equity_risk_premium', 0.055):.1%}")
        snap_table.add_row("DCF projection years",    str(dcf_cfg.get("projection_years", 7)))
        snap_table.add_row("Terminal growth rate",    f"{dcf_cfg.get('terminal_growth_rate', 0.03):.1%}")
        snap_table.add_row("MC simulations (analyze)",str(mc_cfg.get("n_simulations", 10000)))
        snap_table.add_row("MC simulations (screen)", str(mc_cfg.get("n_simulations_screen", 1000)))
        snap_table.add_row("Shortlist threshold",     str(sc_cfg.get("shortlist_threshold", 60)))
        snap_table.add_row("Top N shown",             str(config.get("output", {}).get("top_n", 20)))
        console.print(snap_table)
        console.print()

    def print_portfolio(self, holdings: dict, scored: list[dict], suggestions: list[dict]) -> None:
        console.print(f"\n[bold {GOLD}]Current Portfolio[/bold {GOLD}]")
        t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {GOLD}")
        t.add_column("Ticker")
        t.add_column("Shares", justify="right")
        t.add_column("Score", justify="right")
        t.add_column("Upside", justify="right")

        scored_map = {r["ticker"]: r for r in scored}
        for ticker, shares in holdings.items():
            r = scored_map.get(ticker, {})
            t.add_row(
                ticker,
                str(shares),
                f"{r.get('score', 0):.1f}" if r else "—",
                f"{r.get('dcf_upside_pct', 0):+.1f}%" if r else "—",
            )
        console.print(t)

        if suggestions:
            console.print(f"\n[bold]Rebalancing Suggestions[/bold]")
            for s in suggestions:
                console.print(f"  {s['action']:8s} {s['ticker']}  {s.get('reason', '')}")
