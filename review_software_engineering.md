# Sanctum Equity Screener — Senior SWE Review

**Reviewer:** Senior Software Engineer (15+ years xp)
**Focus:** CLI Ergonomics, Workflow Automation, and Portfolio Smoothness

---

## 1. Architectural Integrity & Reliability

### The Testing Foundation
The `test_models.py` file is excellent—comprehensive, hermetic (no API calls), and uses a robust factory pattern for mocks. 
*   **Status:** GREEN. 
*   **Recommendation:** We should add `test_cli.py` to ensure the sub-command parsing and config overrides don't regress as we make the CLI more complex.

### Dependency Management
*   The `try/except` block in `sanctum.py` for `yaml` is a good "fail-friendly" touch for new users.
*   **Recommendation:** Move to a `src/` layout or use a proper `pyproject.toml` to make `pip install -e .` the standard dev workflow. This makes the `sanctum` command available globally in the venv.

---

## 2. CLI Ergonomics: "The Guys" Test

Currently, the tool requires a lot of "Text Editor Friction" (editing `config.yaml` or a CSV). To make this smooth for the club, we need to move toward **interactive persistence**.

### Problem: Portfolio Friction
The `portfolio --holdings path/to/csv` is a "stateless" approach. Every time they run it, they have to point to a file.
*   **Fix:** Introduce a `sanctum portfolio init` and `sanctum portfolio add AAPL 10` workflow. Store the holdings in a hidden `.sanctum/holdings.json` or within the `.cache/sanctum.db`.

### Problem: Ticker Entry
Typing `sanctum screen --tickers AAPL,GOOG,TSM` is tedious for a long list.
*   **Fix:** Support a "Watchlist" concept. `sanctum watchlist add TSM` saves it to a local list that `sanctum screen` uses by default.

### Proposed Command Structure Refinement

1.  `sanctum init` — Interactive setup (API keys, default universe, creating local cache).
2.  `sanctum search <query>` — Quick fetch of a ticker's basic stats to verify before adding to portfolio.
3.  `sanctum portfolio [add|remove|show|rebalance]` — Full management of the group's positions without touching a CSV.
4.  `sanctum screen [--sp500|--nasdaq|--watchlist]` — One-flag universe selection.

---

## 3. Workflow & Debugging

### The "Black Box" Problem
When a stock fails (Finding 6.2 in Math review), the user just sees a neutral score.
*   **Fix:** Implement a `--verbose` or `--debug` flag that prints the full `errors` list and the Bayesian update trace directly to the terminal for the specific stock that failed.

### Portfolio Rebalancing
The current `suggest_rebalance` is a simple heuristic.
*   **Enhancement:** Allow the user to specify a "Target Allocation" (e.g., "I want 10% in Semis"). The rebalancer should look at the `sector` metadata already in `StockData` and suggest trims to stay within sector limits.

---

## 4. Next Steps Implementation Plan

1.  **Refactor `sanctum.py`** to use `rich.console` for all output, making the CLI feel like a modern app (colors, tables, progress bars).
2.  **Implement `PortfolioManager`** class to handle persistent state (holdings) in the SQLITE cache, removing the CSV requirement.
3.  **Add `watchlist` subcommands** to manage the "Universe of Interest" easily.
4.  **Interactive Overrides:** If a required parameter is missing, use `rich.prompt` to ask the user instead of crashing with an error.

---

**SWE Conclusion:** The engine is Ferrari-grade, but the dashboard is a terminal from 1995. Let's build a cockpit "the guys" actually want to sit in.
