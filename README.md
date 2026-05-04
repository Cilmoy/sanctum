# Sanctum Equity Screener

Institutional-grade quantitative screening and analysis terminal for public equities. This tool implements a multi-stage valuation pipeline including WACC, DCF, Monte Carlo, and Bayesian sequential updating to identify fundamental mispricing and short-term catalyst velocity.

## Installation

### Method 1: Homebrew (Recommended for macOS)

```bash
brew tap cilmoy/sanctum
brew install sanctum
```

### Method 2: Manual Setup

**Requirements:** Python 3.10+

1. Clone the repository:
   ```bash
   git clone https://github.com/Cilmoy/sanctum.git
   cd sanctum
   ```
2. Run the initialization script:
   ```bash
   ./cli_launcher.sh setup
   ```
3. Run the application:
   ```bash
   sanctum
   ```

## Usage

### Watchlist Management
Maintain a persistent universe of interest across sessions.
```bash
sanctum watchlist add TSM
sanctum watchlist list
```

### Screening
Screen the watchlist or a broad index using fundamental and technical filters.
```bash
# Screen saved watchlist
sanctum screen

# Screen S&P 500 constituents
sanctum screen --set universe.source=sp500
```

### Deep-Dive Analysis
Perform detailed mathematical analysis of a single ticker, including sensitivity analysis and options strategy recommendations.
```bash
sanctum analyze NVDA
```

### Portfolio Management
Track actual holdings and generate rebalancing suggestions based on live conviction scores.
```bash
sanctum portfolio add AAPL 10 185.50
sanctum portfolio rebalance
```

## Methodology

### The "Value & Velocity" Model
Stocks are evaluated on two primary dimensions:
- **Fundamental Value:** Intrinsic value derived from a 2-stage Free Cash Flow model with Monte Carlo uncertainty simulation.
- **Catalyst Velocity:** Short-term momentum signals including insider trading clusters, earnings acceleration, and volatility squeeze detection.

### Trade Archetypes
- **Asymmetric Convexity:** Deep fundamental floor combined with high-velocity timing.
- **Convergence:** Strategic alignment between long-term value and short-term momentum.

## Configuration
Model assumptions and universe parameters are managed in `sanctum/config.yaml`. All settings can be overridden at runtime using the `--set` flag.

## Project Roadmap
- **Phase 4:** Institutional Risk & Sizing (Kelly Criterion integration).
- **Phase 5:** PDF Research Report Engine.
