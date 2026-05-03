# SANCTUM — Parameter Reference

Plain-English explanations of every setting in `sanctum/config.yaml`.
All parameters can be overridden for a single run using `--set KEY=VALUE` without editing the file.

---

## Universe — Which stocks to screen

These filters control what goes into the funnel before any scoring happens.

| Parameter | Default | What it does |
|---|---|---|
| `universe.source` | `sp500` | Which universe to pull. `sp500` = S&P 500, `nasdaq100` = Nasdaq-100, `all_us` = all NYSE/NASDAQ/AMEX listed stocks (~8,000), `custom` = only the tickers you list below. |
| `universe.custom_tickers` | `[]` | The specific tickers to screen when source is `custom`. Example: `["GOOG", "NVDA", "TSM"]` |
| `universe.min_market_cap_B` | `2` | Minimum company size in billions of dollars. Set to 2 to exclude micro-caps where data is often unreliable and liquidity is thin. Raise to 10 if you only want large-caps. |
| `universe.min_avg_volume_M` | `1` | Minimum average daily trading volume in millions of shares. Filters out stocks that are hard to buy or sell at a fair price. |
| `universe.exclude_sectors` | `[]` | Sectors to skip entirely. Example: `["Utilities", "Real Estate"]` to exclude dividend-yield plays that don't fit a growth-oriented portfolio. |

---

## WACC — The discount rate

WACC (Weighted Average Cost of Capital) is the minimum return a company needs to generate to be worth investing in. Think of it as the "hurdle rate" — if a company's projected returns don't clear this bar, it's not an attractive investment at the current price. A higher WACC makes stocks look *less* valuable; a lower WACC makes them look *more* valuable.

| Parameter | Default | What it does |
|---|---|---|
| `wacc.risk_free_rate` | `0.043` (4.3%) | The return you could get with zero risk — typically the 10-year US Treasury yield. Update this when interest rates change materially. As of 2024–2025, this is in the 4.0–4.5% range. |
| `wacc.equity_risk_premium` | `0.055` (5.5%) | The extra return investors demand for holding stocks instead of Treasury bonds. This is the "price of risk" for the overall stock market. 5.5% is the long-run Damodaran estimate. Raising this makes all stocks look more expensive. |
| `wacc.small_cap_premium` | `0.025` (2.5%) | An additional return premium required for smaller companies, which are riskier and less liquid than large-caps. Only applied to companies below `small_cap_threshold_B`. |
| `wacc.small_cap_threshold_B` | `5` | Market cap cutoff in billions. Companies below $5B get the small-cap premium added to their WACC. |
| `wacc.cost_of_debt_fallback` | `0.055` (5.5%) | The interest rate used for a company's debt when the model can't calculate it from the financial statements. This is only a fallback — normally the model derives it from actual interest expense. |
| `wacc.marginal_tax_rate` | `0.21` (21%) | The corporate tax rate. Interest on debt is tax-deductible, so this reduces the effective cost of debt. Reflects the current US federal corporate tax rate. |

**Practical guidance:** If you believe interest rates will stay higher for longer, raise `risk_free_rate`. If you think the market is pricing risk too cheaply (i.e., stocks are overvalued broadly), raise `equity_risk_premium`.

---

## DCF — The base-case valuation

The DCF (Discounted Cash Flow) model projects how much cash a company will generate over the next several years, then calculates what all that future cash is worth in today's dollars. The result is an "implied price" — what the stock should theoretically trade at based on its fundamentals.

| Parameter | Default | What it does |
|---|---|---|
| `dcf.projection_years` | `7` | How many years to project the company's cash flows before switching to the "steady state" terminal value. 5–10 years is standard. Longer projections are more speculative. |
| `dcf.terminal_growth_rate` | `0.03` (3%) | The rate at which the company's cash flows are assumed to grow *forever* after the projection period ends. This is typically set near long-run GDP growth (2–3%). **This is the most important number in the model** — small changes here have an outsized impact on the implied price. Never set this above the WACC, or the math breaks. |
| `dcf.growth_override` | `{}` | Manually set the revenue growth rate for each projection year for a specific stock. Overrides the model's automatic estimate. Example: `{"NVDA": [0.40, 0.28, 0.20, 0.15, 0.10, 0.08, 0.06]}` sets NVDA's growth to 40% in year 1, 28% in year 2, etc. |
| `dcf.margin_override` | `{}` | Same as above but for FCF margin (how much of each revenue dollar becomes free cash flow). Useful when you have a strong view on a company's cost structure. |

**Practical guidance:** The terminal growth rate drives 70–85% of the implied price in a typical DCF. Be conservative — most companies don't grow faster than the economy forever. If a stock's DCF only looks good because of an aggressive terminal growth assumption, treat it skeptically.

---

## Monte Carlo — The probability distribution

Instead of running one DCF with one set of assumptions, Monte Carlo runs the same model thousands of times with slightly different assumptions each time, drawn randomly. The result is a distribution of possible outcomes — a range of implied prices rather than a single number. This is more honest than a single point estimate.

| Parameter | Default | What it does |
|---|---|---|
| `montecarlo.n_simulations` | `10000` | Number of simulation runs for the `analyze` command. More runs = more precise distribution, but slower. 10,000 is a good balance. |
| `montecarlo.n_simulations_screen` | `1000` | Number of runs used during bulk `screen` for speed. Less precise but fast enough to rank stocks. |
| `montecarlo.seed` | `42` | A number that locks in the random draws so results are reproducible. Same data + same seed = identical output every time. Change this to get a different random sample, but keep it fixed for consistency. |
| `montecarlo.revenue_vol` | `0.08` (8%) | How uncertain we are about the company's *starting* revenue level. Higher = wider distribution of outcomes. |
| `montecarlo.growth_vol` | `0.06` (6%) | How much each year's revenue growth rate can deviate from the base case. Higher = more year-to-year variability in the simulated growth path. |
| `montecarlo.margin_vol` | `0.03` (3%) | How much the company's profit margin can vary from the base assumption. |
| `montecarlo.terminal_growth_vol` | `0.005` (0.5%) | Uncertainty around the long-term terminal growth rate. Kept small because this assumption is already inherently uncertain — adding more noise here inflates the distribution artificially. |
| `montecarlo.antithetic_variates` | `true` | A technical setting that makes the simulation more efficient by pairing each random draw with its mirror image. Halves the statistical error of the result for the same number of runs. Leave this on. |

**Reading the output:** The P50 (median) is the "most likely" outcome. P25–P75 is where the middle half of outcomes fall. P5 and P95 are the extreme tails. `P(price > current)` is the probability the model assigns that the stock is undervalued — above 60% is generally interesting.

---

## Bayesian — Probability of bull/base/bear

The Bayesian model starts with a prior belief about the stock (25% bull, 50% base, 25% bear by default), then updates that belief as it processes each piece of evidence — revenue growth, margins, valuation, analyst targets, and earnings history. The output is a revised probability for each scenario, and an expected value that blends them together.

### Prior probabilities

| Parameter | Default | What it does |
|---|---|---|
| `bayesian.prior.bull` | `0.25` | Starting probability of the bull scenario (stock significantly outperforms) before looking at any company-specific data. |
| `bayesian.prior.base` | `0.50` | Starting probability of the base scenario (stock performs roughly in line with expectations). |
| `bayesian.prior.bear` | `0.25` | Starting probability of the bear scenario (stock disappoints). |

The priors must sum to 1. The default is slightly pessimistic — base case is most likely, and bull/bear are symmetric. If you believe the overall market environment is more favorable, you could shift bull higher.

| Parameter | Default | What it does |
|---|---|---|
| `bayesian.likelihood_clip` | `[0.05, 0.95]` | Prevents any single piece of evidence from completely ruling out a scenario. Even a very positive earnings beat shouldn't eliminate the possibility of a bear outcome — there are always unknown risks. |

### Evidence factors

Each factor below looks at a specific metric for the stock, classifies it into a bucket (e.g., "high growth" or "declining margins"), and uses that to update the bull/base/bear probabilities. The likelihoods `[bull, base, bear]` represent: "if we're in scenario X, how likely is it we'd see this signal?" These are calibrated judgment calls, not derived from data.

**`revenue_growth`** — Year-over-year revenue growth rate

| Bucket | Threshold | Interpretation |
|---|---|---|
| High | > 20% | Strong growth; bullish signal |
| Moderate | 5–20% | Healthy but not exceptional |
| Low | 0–5% | Stagnating |
| Decline | < 0% | Revenue shrinking; bearish signal |

**`gross_margin`** — Gross profit as a % of revenue (pricing power indicator)

| Bucket | Threshold | Interpretation |
|---|---|---|
| High | > 60% | Strong pricing power; software/pharma territory |
| Moderate | 40–60% | Competitive but defensible |
| Low | < 40% | Commoditized or high-cost business |

**`forward_pe`** — Forward price-to-earnings ratio (valuation signal)

| Bucket | Threshold | Interpretation |
|---|---|---|
| Attractive | < 22× | Cheap relative to earnings |
| Fair | 22–35× | Reasonable for a quality company |
| Elevated | 35–60× | Priced for a lot of growth |
| Extreme | > 60× | Requires near-perfect execution to justify |

**`analyst_upside`** — Wall Street consensus price target vs. current price

| Bucket | Threshold | Interpretation |
|---|---|---|
| Strong | > 30% upside | Analysts see significant room to run |
| Moderate | 10–30% upside | Mild positive consensus |
| Slim | 0–10% upside | Analysts roughly neutral |
| Downside | Negative | Analysts think it's overvalued |

**`earnings_surprise`** — Most recent quarter: did earnings beat or miss expectations?

| Bucket | Threshold | Interpretation |
|---|---|---|
| Beat | > 10% above estimate | Strong execution signal |
| Inline | 0–10% | Met expectations |
| Miss | Below estimate | Execution concern |

---

## Sensitivity — How much does revenue matter?

| Parameter | Default | What it does |
|---|---|---|
| `sensitivity.revenue_delta_pct` | `5` | Re-runs the DCF with revenue 5% higher (bull case) and 5% lower (bear case). Shows how much the implied price moves per 1% change in revenue. A high sensitivity means the valuation is heavily dependent on hitting revenue targets — a warning sign for uncertain businesses. |

---

## Scoring — How the 0–100 score is calculated

| Parameter | Default | What it does |
|---|---|---|
| `scoring.weights.bayesian_upside` | `0.30` | Weight given to the Bayesian expected value upside in the composite score. |
| `scoring.weights.mc_upside` | `0.25` | Weight given to the Monte Carlo median upside. |
| `scoring.weights.dcf_upside` | `0.20` | Weight given to the base-case DCF upside. |
| `scoring.weights.earnings_momentum` | `0.15` | Weight given to recent earnings beat/miss and analyst estimate revisions. |
| `scoring.weights.margin_trend` | `0.10` | Weight given to whether gross margins are expanding or contracting over the last 3 years. |
| `scoring.shortlist_threshold` | `60` | **The shortlist cutoff.** Any stock scoring at or above this number appears in the shortlist. Score of 50 = roughly zero upside. Score of 60 = ~10% model upside. Score of 75+ = high conviction. Raise this to tighten the shortlist; lower it to cast a wider net. |

**All weights must sum to 1.0.** If you change them, make sure they still add up.

---

## Portfolio — Position limits

These parameters are used by the `portfolio` command to check whether your current holdings violate concentration limits and to size new positions.

| Parameter | Default | What it does |
|---|---|---|
| `portfolio.max_positions` | `15` | Maximum number of stocks to hold at once. Keeps the portfolio concentrated enough to matter but diversified enough to limit single-stock risk. |
| `portfolio.min_positions` | `5` | Minimum number of stocks. Prevents over-concentration into just one or two names. |
| `portfolio.max_cash_pct` | `15` | Maximum percentage of the portfolio that can sit in cash. Forces the model to stay invested. |
| `portfolio.max_single_position_pct` | `20` | No single stock can exceed 20% of portfolio value. Hard ceiling on individual position size. |
| `portfolio.max_sector_pct` | `40` | No single GICS sector can exceed 40% of portfolio value. Prevents over-concentration in, say, technology. |
| `portfolio.max_semi_pct` | `50` | Hard cap on semiconductor exposure specifically, given the club's history of holding concentrated chip positions (NVDA, INTC, TSM, AVGO). |

---

## Output

| Parameter | Default | What it does |
|---|---|---|
| `output.top_n` | `20` | How many stocks to show in the `screen` results table. Doesn't affect scoring — just controls how many rows are printed. |
| `output.show_math` | `true` | Whether the `analyze` command prints the full derivation: WACC calculation, DCF year-by-year table, Monte Carlo percentile table, and Bayesian update trace. Set to `false` for a quick summary-only view. |
| `output.export_pdf` | `false` | PDF export (Phase 2 — not yet active). |
| `output.brand_name` | `"SANCTUM LLC"` | Name shown in the header of all output. |

---

## Cache

| Parameter | Default | What it does |
|---|---|---|
| `cache.enabled` | `true` | Whether to cache yfinance data locally. Highly recommended — fetching 500 stocks takes minutes; using the cache takes seconds. |
| `cache.ttl_hours` | `24` | How long cached data is considered fresh, in hours. After 24 hours, the next run re-fetches from yfinance. Set lower (e.g., `1`) if you need real-time data; set higher (e.g., `168` = 1 week) if you're doing analysis over several sessions. |
| `cache.db_path` | `.cache/sanctum.db` | Where the cache file is stored on disk, relative to the `sanctum/` directory. |
