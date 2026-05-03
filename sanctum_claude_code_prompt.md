# SANCTUM LLC — Equity Screening & Analysis CLI
# Claude Code Session Prompt
# ──────────────────────────────────────────────

## Project Overview

Build a Python CLI tool called `sanctum` that screens, scores, and ranks public equities 
using a disciplined quantitative framework. The goal is to find alpha in a repeatable, 
parameter-driven way for a small investment club portfolio (5–15 stocks, up to 15% cash, 
$1K–$5K annual contributions).

This is the screening and idea-generation layer. It should take a universe of tickers 
(or pull from an index like S&P 500), run each through a standardized quantitative pipeline, 
and output a ranked list with conviction scores. Think of it as a funnel: 
broad universe → quantitative filter → scored shortlist → portfolio-ready candidates.

## Architecture

```
sanctum/
├── config.yaml              # All tunable parameters in one place
├── sanctum.py               # CLI entry point (argparse or click)
├── data/
│   ├── fetcher.py           # Market data retrieval (yfinance or similar)
│   └── cache.py             # Local caching to avoid redundant API calls
├── models/
│   ├── wacc.py              # WACC derivation (CAPM + size premium)
│   ├── dcf.py               # Multi-year DCF with terminal value
│   ├── montecarlo.py        # Monte Carlo simulation (configurable n paths)
│   ├── bayesian.py          # Bayesian scoring with configurable evidence factors
│   └── sensitivity.py       # Revenue ±X% beat/miss analysis
├── scoring/
│   ├── composite.py         # Weighted composite score from all models
│   └── filters.py           # Pre-screen filters (min market cap, liquidity, etc.)
├── portfolio/
│   ├── holdings.py          # Current portfolio state (read from CSV or YAML)
│   ├── allocation.py        # Position sizing and concentration checks
│   └── rebalance.py         # Suggested trades given new scores + existing positions
├── output/
│   ├── terminal.py          # Rich terminal output (tables, sparklines)
│   └── pdf_report.py        # Optional branded PDF export (ReportLab)
└── tests/
    └── test_models.py       # Sanity checks on model outputs
```

## config.yaml — The Control Panel

This is the most important file. Every tunable parameter lives here so the user 
never has to edit Python code to adjust the model. Example structure:

```yaml
# ── UNIVERSE ──
universe:
  source: "sp500"              # sp500, nasdaq100, or custom ticker list
  custom_tickers: []           # override: ["GOOG", "NVDA", "TSM", ...]
  min_market_cap_B: 2          # minimum market cap in billions
  min_avg_volume_M: 1          # minimum avg daily volume in millions
  exclude_sectors: []          # e.g. ["Utilities", "Real Estate"]

# ── WACC ──
wacc:
  risk_free_rate: 0.043        # 10Y UST yield
  equity_risk_premium: 0.055   # Damodaran implied ERP
  small_cap_premium: 0.025     # Duff & Phelps; applied when mkt cap < threshold
  small_cap_threshold_B: 5     # below this market cap, add SCP
  cost_of_debt: 0.055          # default pre-tax Kd
  marginal_tax_rate: 0.21

# ── DCF ──
dcf:
  projection_years: 7
  terminal_growth_rate: 0.03
  # Revenue growth and FCF margin assumptions are derived per-stock
  # from historical trends + sector medians, but you can override:
  growth_override: {}          # e.g. {"NVDA": [0.40, 0.28, 0.20, 0.15, 0.10, 0.08, 0.06]}
  margin_override: {}

# ── MONTE CARLO ──
montecarlo:
  n_simulations: 10000
  seed: 42                     # reproducibility
  revenue_vol: 0.08            # std dev of initial revenue draw
  growth_vol: 0.06             # std dev of annual growth shock
  margin_vol: 0.03             # std dev of terminal margin
  terminal_growth_vol: 0.005

# ── BAYESIAN ──
bayesian:
  prior:
    bull: 0.25
    base: 0.50
    bear: 0.25
  # Evidence factors with likelihood ratios [bull, base, bear]
  # These are the default factors; each is auto-evaluated per stock
  evidence_factors:
    revenue_growth:
      thresholds: [0.20, 0.05, 0.0]         # high, moderate, low, decline
      likelihoods:
        high:     [0.80, 0.50, 0.30]
        moderate: [0.60, 0.70, 0.40]
        low:      [0.40, 0.60, 0.60]
        decline:  [0.20, 0.40, 0.80]
    gross_margin:
      thresholds: [0.60, 0.40]
      likelihoods:
        high:     [0.75, 0.65, 0.40]
        moderate: [0.60, 0.65, 0.50]
        low:      [0.40, 0.55, 0.70]
    forward_pe:
      thresholds: [22, 35, 60]
      likelihoods:
        attractive: [0.80, 0.60, 0.30]
        fair:       [0.60, 0.70, 0.50]
        elevated:   [0.40, 0.50, 0.70]
        extreme:    [0.30, 0.40, 0.80]
    analyst_upside:
      thresholds: [0.30, 0.10, 0.0]
      likelihoods:
        strong:   [0.80, 0.60, 0.30]
        moderate: [0.65, 0.65, 0.45]
        slim:     [0.50, 0.60, 0.60]
        downside: [0.30, 0.40, 0.80]
    earnings_surprise:
      thresholds: [0.10, 0.0]               # last quarter EPS surprise %
      likelihoods:
        beat:     [0.75, 0.60, 0.35]
        inline:   [0.55, 0.65, 0.55]
        miss:     [0.30, 0.45, 0.75]

# ── SENSITIVITY ──
sensitivity:
  revenue_delta_pct: 5         # ±5% beat/miss

# ── COMPOSITE SCORING WEIGHTS ──
scoring:
  weights:
    bayesian_upside: 0.30      # E[V] vs current price
    mc_upside: 0.25            # MC median vs current price
    dcf_upside: 0.20           # DCF base case vs current price
    margin_trend: 0.10         # 3-year gross margin trajectory
    earnings_momentum: 0.15    # recent EPS surprise + revision trend
  # Final score is 0–100; only stocks above threshold are shortlisted
  shortlist_threshold: 60

# ── PORTFOLIO CONSTRAINTS ──
portfolio:
  max_positions: 15
  min_positions: 5
  max_cash_pct: 15
  max_single_position_pct: 20
  max_sector_pct: 40
  max_semi_pct: 50             # hard cap on semiconductor exposure

# ── OUTPUT ──
output:
  top_n: 20                    # show top N candidates
  show_math: true              # print WACC/DCF derivation for top picks
  export_pdf: false            # generate branded SANCTUM PDF
  brand_name: "SANCTUM LLC"
```

## CLI Interface

```bash
# Screen the full S&P 500 and show top 20
python sanctum.py screen

# Screen a custom list
python sanctum.py screen --tickers GOOG,NVDA,TSM,INTC,AVGO,MSFT,AAPL

# Deep-dive a single stock (full model output with math)
python sanctum.py analyze NVDA

# Compare two stocks head-to-head
python sanctum.py compare INTC TSM

# Show current portfolio and rebalancing suggestions
python sanctum.py portfolio --holdings portfolio.csv

# Adjust a parameter on the fly without editing config
python sanctum.py screen --set montecarlo.n_simulations=5000
python sanctum.py screen --set scoring.shortlist_threshold=70

# Export results
python sanctum.py screen --export pdf
python sanctum.py analyze GOOG --export pdf
```

## Composite Scoring Logic (scoring/composite.py)

Each stock gets a score from 0–100 computed as follows:

1. **Bayesian Upside (30%)**: Expected value from Bayesian posterior-weighted 
   scenario targets vs current price. Normalized to 0–100 scale where 0% upside = 50, 
   +50% upside = 100, -50% downside = 0.

2. **Monte Carlo Upside (25%)**: MC median implied price vs current price. 
   Same normalization.

3. **DCF Upside (20%)**: Base-case DCF implied price vs current price. 
   Same normalization.

4. **Margin Trend (10%)**: 3-year gross margin slope. Expanding margins = higher score.

5. **Earnings Momentum (15%)**: Composite of last-quarter EPS surprise magnitude + 
   3-month EPS revision trend (are analysts raising or cutting estimates?).

Final score = weighted sum, clamped to [0, 100].

## The Standard Analysis Pipeline (per stock)

This is the same methodology we developed for York Space Systems 
and then applied to the 9-stock portfolio:

1. **Data Pull**: Price, financials (5Y revenue, margins, EPS), analyst targets, 
   beta, market cap, sector, recent earnings surprise.

2. **WACC Derivation**: CAPM with size premium. Show the math:
   Ke = Rf + β × MRP + SCP, then WACC = wE×Ke + wD×Kd(1-t)

3. **DCF Model**: N-year projection with sector-appropriate growth curves 
   and margin ramps. Gordon Growth terminal value. Equity bridge 
   (EV - debt + cash = equity value / shares = implied price).

4. **Monte Carlo**: Stochastic revenue, growth, terminal margin, terminal growth. 
   N simulations. Output percentile table (P5, P25, P50, P75, P95) 
   and probability metrics (P(price > current), P(price > target)).

5. **Bayesian Framework**: Start from configurable priors. Sequentially update 
   on each evidence factor. Output posterior probabilities for bull/base/bear.

6. **Expected Value**: E[V] = P(bull)×PT_bull + P(base)×PT_base + P(bear)×PT_bear

7. **Sensitivity**: ±X% revenue beat/miss impact on DCF implied value.

8. **Composite Score**: Weighted combination → 0–100 conviction score.

## Data Source

Use `yfinance` as the primary data source. It's free, pip-installable, and gives us:
- Current price, market cap, beta, sector
- Historical financials (income statement, balance sheet)
- Analyst price targets
- Earnings history (for surprise calculation)

Cache results locally (SQLite or pickle) with a configurable TTL 
so we're not hammering the API on repeat runs.

## Terminal Output Style

Use `rich` library for terminal output. Dark theme, gold accent (#c9a84c) 
where possible. Tables should be clean and scannable. Example:

```
SANCTUM LLC — Equity Screener
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Universe: S&P 500 | Filtered: 347 | Scored: 347 | Shortlisted: 23

 Rank  Ticker  Score  E[V]     Cur     Upside  MC P50   Bayes    Sector
 ───── ─────── ────── ──────── ─────── ─────── ──────── ──────── ──────────
  1    GOOG    87.3   $412     $382    +7.8%   $425     B:42%    Comm Svc
  2    NVDA    84.1   $248     $198    +25.3%  $261     B:38%    Semis
  3    TSM     81.6   $445     $398    +11.8%  $460     B:35%    Semis
 ...
```

## Important Implementation Notes

- **Reproducibility**: All random processes must be seeded. 
  Given the same config and data, the output must be identical.
- **Show your work**: When `show_math: true`, print the WACC derivation, 
  DCF table, MC percentiles, and Bayesian update trace for each shortlisted stock. 
  This is how we verify the model isn't doing something insane.
- **Fail gracefully**: If data is missing for a stock (no analyst target, 
  no earnings history), skip that evidence factor in Bayesian rather than crashing. 
  Log what was skipped.
- **Speed**: The full S&P 500 screen with 10K MC sims per stock will be slow. 
  Add a progress bar. Consider reducing MC to 1000 for screening and 10000 for 
  deep dives (`analyze` command).
- **No hardcoded assumptions**: Everything flows from config.yaml. 
  If I want to change the equity risk premium or add a new Bayesian factor, 
  I edit the YAML — not the Python.

## Phase 1 Deliverables (this session)

1. Project scaffolding with all files created
2. config.yaml with sensible defaults
3. Working `screen` command for a custom ticker list (data fetching + all 5 models + scoring)
4. Working `analyze` command for single-stock deep dive with full math output
5. Working `compare` command for head-to-head
6. Basic test coverage on the quant models

Start building. Prioritize getting the pipeline working end-to-end for a small 
ticker list first (e.g. GOOG, NVDA, INTC, TSM, AVGO), then expand to full universe.
