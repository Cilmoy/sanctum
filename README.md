# SANCTUM LLC — Equity Screener & Analysis CLI

A quantitative stock screening and analysis tool for the Sanctum investment club. It pulls financial data, runs a standardized valuation pipeline (WACC, DCF, Monte Carlo, Bayesian), and identifies high-conviction "Multi-Bagger" opportunities.

---

## 🚀 One-Command Setup

**Requirements:** Python 3.10+ (macOS/Linux). No manual venv or pip steps required.

1.  **Clone the repo**
2.  **Run setup:**
    ```bash
    ./cli_launcher.sh setup
    ```
3.  **Use the tool globally:**
    ```bash
    sanctum help
    ```
    *The setup command automatically creates a virtual environment, installs all dependencies, and creates a global 'sanctum' command in your PATH.*

---

## 🛠 Command Dashboard

### 1. The Watchlist (Persistent)
Instead of typing tickers every time, save your "Universe of Interest."
```bash
sanctum watchlist add TSM
sanctum watchlist add GOOG
sanctum watchlist list
```

### 2. Screening
Rank your watchlist or a broad index.
```bash
# Screens your saved watchlist by default
sanctum screen

# Screen the S&P 500
sanctum screen --set universe.source=sp500

# Quick screen for specific tickers
sanctum screen --tickers NVDA,AVGO,ARM
```

### 3. Deep-Dive Analysis
Full mathematical derivation for a single stock.
```bash
sanctum analyze TSM
```
*Use the `--debug` flag to see the Bayesian update trace if the score looks unusual.*

### 4. Portfolio Tracking
Manage the club's actual positions and get rebalancing suggestions.
```bash
# Add a position (Ticker, Shares, Avg Cost)
sanctum portfolio add TSM 50 145.20

# Review portfolio and get rebalance advice
sanctum portfolio rebalance
```

---

## 🧠 The "Value & Velocity" Score

Every stock gets a **conviction score (0–100)**. It identifies the intersection of:
- **Value:** Deep fundamental upside in the DCF/Monte Carlo models.
- **Velocity:** High catalyst scores (earnings beats, short interest, insider buying).

### Trade Archetypes:
- **CONVERGENCE (10x Potential):** Extreme fundamental mispricing + violent catalyst momentum.
- **STRONG BUY:** Value and momentum are aligned.
- **MOMENTUM SETUP:** Driven by catalysts; size small, tight stop.

---

## ⚙️ Configuration
All parameters live in `sanctum/config.yaml`. 

**On-the-fly overrides:**
```bash
sanctum screen --set scoring.shortlist_threshold=75
sanctum analyze NVDA --set dcf.terminal_growth_rate=0.04
```

---

## 📂 File Structure
- `sanctum/config.yaml`: The "Brain" (all model assumptions).
- `sanctum/sanctum.py`: The "Cockpit" (CLI entry point).
- `sanctum/models/`: The "Engine" (DCF, WACC, Bayesian, etc.).
- `sanctum/data/`: The "Fuel" (yfinance fetcher and SQLite DB).
