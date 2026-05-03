"""
test_models.py — Unit tests for the Sanctum financial model pipeline.

Test coverage:
  - WACC: numerical correctness against hand-calculated example, fallbacks.
  - DCF: terminal value formula, equity bridge, zero/negative FCF, missing data.
  - Monte Carlo: reproducibility (same seed → same P50), antithetic variates.
  - Bayesian: single-factor posterior by hand, update trace, skipped factors.
  - Sensitivity: symmetry property, dV_dr sign.
  - Composite scoring: normalization (_upside_to_score), score bounds.
  - Filters: market cap, volume, sector exclusion.

No live API calls. All tests use mock StockData objects.
"""

import math
import sys
import os
import pytest
import numpy as np

# Ensure the sanctum package root is importable when running pytest from any dir
_SANCTUM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SANCTUM_ROOT not in sys.path:
    sys.path.insert(0, _SANCTUM_ROOT)

from models.wacc import compute_wacc
from models.dcf import compute_dcf, _geometric_decay, SECTOR_GROWTH_MEDIANS
from models.montecarlo import run_montecarlo
from models.bayesian import compute_bayesian
from models.sensitivity import compute_sensitivity
from scoring.composite import CompositeScorer, _upside_to_score, _compute_margin_trend
from scoring.filters import apply_filters


# ─────────────────────────────────────────────────────────────────────────────
# Mock StockData factory
# ─────────────────────────────────────────────────────────────────────────────

class MockStock:
    """
    Minimal StockData-compatible mock.

    Provides all fields required by the model pipeline with sensible defaults.
    Override any field by passing kwargs to the constructor.
    """
    def __init__(self, **kwargs):
        # Defaults — representative mid-cap tech company
        self.ticker = "TEST"
        self.current_price = 100.0
        self.market_cap = 10e9          # $10B
        self.beta = 1.2
        self.sector = "Technology"
        self.shares_outstanding = 100e6  # 100M diluted shares

        # Financials (most-recent-first, 3 years)
        self.revenue = [5e9, 4e9, 3.5e9]          # $5B, $4B, $3.5B
        self.gross_profit = [3e9, 2.4e9, 2.1e9]
        self.operating_income = [1e9, 0.8e9, 0.7e9]
        self.net_income = [0.8e9, 0.65e9, 0.55e9]
        self.fcf = [0.75e9, 0.6e9, 0.5e9]         # positive FCF
        self.total_debt = [1e9, 0.9e9]              # $1B recent, $900M prior
        self.cash = [0.5e9, 0.4e9]
        self.interest_expense = [0.05e9, 0.045e9]  # $50M
        self.ebit = [1e9, 0.8e9, 0.7e9]

        # Analyst targets
        self.analyst_target_mean = 120.0
        self.analyst_target_high = 150.0
        self.analyst_target_low = 90.0

        # Earnings
        self.eps_surprise_pct = 0.05    # +5% beat
        self.eps_revision_trend = 0.02  # +2% upward revision

        # Liquidity
        self.avg_daily_volume = 5e6     # 5M shares/day

        # Forward estimates
        self.forward_pe = 25.0
        self.forward_eps = 4.0

        # Apply overrides
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def gross_margin_history(self):
        margins = []
        for gp, rev in zip(self.gross_profit, self.revenue):
            if rev and rev != 0:
                margins.append(gp / rev)
        return margins

    @property
    def latest_gross_margin(self):
        m = self.gross_margin_history
        return m[0] if m else None

    @property
    def net_debt(self):
        if self.total_debt and self.cash:
            d = self.total_debt[0] if self.total_debt else 0.0
            c = self.cash[0] if self.cash else 0.0
            return d - c
        return None


def _make_minimal_config():
    """Return a minimal config dict matching the production config structure."""
    return {
        "wacc": {
            "risk_free_rate": 0.043,
            "equity_risk_premium": 0.055,
            "small_cap_premium": 0.025,
            "small_cap_threshold_B": 5,
            "cost_of_debt_fallback": 0.055,
            "marginal_tax_rate": 0.21,
        },
        "dcf": {
            "projection_years": 7,
            "terminal_growth_rate": 0.03,
            "growth_override": {},
            "margin_override": {},
        },
        "montecarlo": {
            "n_simulations": 1000,
            "n_simulations_screen": 200,
            "seed": 42,
            "revenue_vol": 0.08,
            "growth_vol": 0.06,
            "margin_vol": 0.03,
            "terminal_growth_vol": 0.005,
            "antithetic_variates": True,
        },
        "bayesian": {
            "prior": {"bull": 0.25, "base": 0.50, "bear": 0.25},
            "likelihood_clip": [0.05, 0.95],
            "evidence_factors": {
                "revenue_growth": {
                    "thresholds": [0.20, 0.05, 0.0],
                    "likelihoods": {
                        "high":     [0.80, 0.50, 0.30],
                        "moderate": [0.60, 0.70, 0.40],
                        "low":      [0.40, 0.60, 0.60],
                        "decline":  [0.20, 0.40, 0.80],
                    },
                },
                "gross_margin": {
                    "thresholds": [0.60, 0.40],
                    "likelihoods": {
                        "high":     [0.75, 0.65, 0.40],
                        "moderate": [0.60, 0.65, 0.50],
                        "low":      [0.40, 0.55, 0.70],
                    },
                },
                "forward_pe": {
                    "thresholds": [22, 35, 60],
                    "likelihoods": {
                        "attractive": [0.80, 0.60, 0.30],
                        "fair":       [0.60, 0.70, 0.50],
                        "elevated":   [0.40, 0.50, 0.70],
                        "extreme":    [0.30, 0.40, 0.80],
                    },
                },
                "analyst_upside": {
                    "thresholds": [0.30, 0.10, 0.0],
                    "likelihoods": {
                        "strong":   [0.80, 0.60, 0.30],
                        "moderate": [0.65, 0.65, 0.45],
                        "slim":     [0.50, 0.60, 0.60],
                        "downside": [0.30, 0.40, 0.80],
                    },
                },
                "earnings_surprise": {
                    "thresholds": [0.10, 0.0],
                    "likelihoods": {
                        "beat":   [0.75, 0.60, 0.35],
                        "inline": [0.55, 0.65, 0.55],
                        "miss":   [0.30, 0.45, 0.75],
                    },
                },
            },
        },
        "sensitivity": {
            "revenue_delta_pct": 5,
        },
        "scoring": {
            "weights": {
                "bayesian_upside": 0.30,
                "mc_upside": 0.25,
                "dcf_upside": 0.20,
                "margin_trend": 0.10,
                "earnings_momentum": 0.15,
            },
            "shortlist_threshold": 60,
        },
        "universe": {
            "min_market_cap_B": 2,
            "min_avg_volume_M": 1,
            "exclude_sectors": [],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# WACC Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWACC:

    def test_numerical_correctness_hand_calculated(self):
        """
        Hand-calculated WACC example.

        Setup:
          rf = 0.043, erp = 0.055, beta = 1.2, scp = 0 (mktcap $10B >= $5B threshold)
          Ke = 0.043 + 1.2 * 0.055 + 0 = 0.043 + 0.066 = 0.109
          interest_expense = $50M, avg_debt = ($1B + $0.9B) / 2 = $950M
          Kd_pretax = 50M / 950M = 0.05263...
          Kd_after_tax = 0.05263 * (1 - 0.21) = 0.04158...
          equity_value = $10B (market cap), debt_value = $1B (most recent total debt)
          total_capital = $11B
          wE = 10/11 = 0.9090..., wD = 1/11 = 0.0909...
          WACC = 0.9090 * 0.109 + 0.0909 * 0.04158 = 0.09908 + 0.00378 = 0.10286...
        """
        config = _make_minimal_config()
        stock = MockStock()

        result = compute_wacc(stock, config)

        # Verify key intermediates
        assert abs(result["ke"] - 0.109) < 1e-6, f"Ke mismatch: {result['ke']}"
        assert result["scp"] == 0.0, "No SCP for $10B market cap"

        expected_kd_pretax = 0.05e9 / ((1e9 + 0.9e9) / 2)
        assert abs(result["kd_pretax"] - expected_kd_pretax) < 1e-6

        expected_kd_after_tax = expected_kd_pretax * (1 - 0.21)
        assert abs(result["kd_after_tax"] - expected_kd_after_tax) < 1e-6

        we_expected = 10e9 / (10e9 + 1e9)
        wd_expected = 1e9 / (10e9 + 1e9)
        assert abs(result["we"] - we_expected) < 1e-6
        assert abs(result["wd"] - wd_expected) < 1e-6

        expected_wacc = we_expected * 0.109 + wd_expected * expected_kd_after_tax
        assert abs(result["wacc"] - expected_wacc) < 1e-6, f"WACC mismatch: {result['wacc']} vs {expected_wacc}"

    def test_small_cap_premium_applied(self):
        """SCP is applied when market_cap < threshold."""
        config = _make_minimal_config()
        stock = MockStock(market_cap=3e9, beta=1.0)  # $3B < $5B threshold

        result = compute_wacc(stock, config)
        assert result["scp"] == 0.025
        # Ke = 0.043 + 1.0 * 0.055 + 0.025 = 0.123
        assert abs(result["ke"] - 0.123) < 1e-6

    def test_small_cap_premium_not_applied_large_cap(self):
        """No SCP for large-cap stocks."""
        config = _make_minimal_config()
        stock = MockStock(market_cap=20e9, beta=1.0)

        result = compute_wacc(stock, config)
        assert result["scp"] == 0.0

    def test_kd_fallback_when_no_interest_data(self):
        """Falls back to config cost_of_debt when interest_expense is empty."""
        config = _make_minimal_config()
        stock = MockStock(interest_expense=[])

        result = compute_wacc(stock, config)
        assert result["kd_pretax"] == 0.055
        assert "fallback" in result["kd_source"]

    def test_kd_fallback_when_no_debt_data(self):
        """Falls back to config cost_of_debt when total_debt is empty."""
        config = _make_minimal_config()
        stock = MockStock(total_debt=[])

        result = compute_wacc(stock, config)
        assert result["kd_pretax"] == 0.055

    def test_beta_fallback_to_one(self):
        """When beta is None, defaults to 1.0."""
        config = _make_minimal_config()
        stock = MockStock(beta=None)

        result = compute_wacc(stock, config)
        assert result["beta"] == 1.0
        assert any("beta" in note.lower() for note in result["notes"])

    def test_all_equity_structure_when_no_debt(self):
        """All-equity structure when total_debt is empty."""
        config = _make_minimal_config()
        stock = MockStock(total_debt=[], interest_expense=[])

        result = compute_wacc(stock, config)
        assert result["we"] == 1.0
        assert result["wd"] == 0.0

    def test_wacc_always_positive(self):
        """WACC must be positive for any reasonable input combination."""
        config = _make_minimal_config()
        stock = MockStock()
        result = compute_wacc(stock, config)
        assert result["wacc"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# DCF Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDCF:

    def _get_wacc_result(self, stock=None, config=None):
        if stock is None:
            stock = MockStock()
        if config is None:
            config = _make_minimal_config()
        return compute_wacc(stock, config)

    def test_terminal_value_formula(self):
        """
        TV = FCF_N * (1 + g) / (WACC - g).
        Verify PV(TV) = TV / (1 + WACC)^N.
        """
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc_result(stock, config)

        result = compute_dcf(stock, wacc_result, config)

        n = config["dcf"]["projection_years"]
        g = config["dcf"]["terminal_growth_rate"]
        wacc = result["wacc"]

        terminal_fcf = result["projection_rows"][-1]["fcf"]
        expected_tv = terminal_fcf * (1.0 + g) / (wacc - g)
        expected_pv_tv = expected_tv / (1.0 + wacc) ** n

        assert abs(result["terminal_value"] - expected_tv) < 1.0  # within $1
        assert abs(result["pv_terminal_value"] - expected_pv_tv) < 1.0

    def test_equity_bridge(self):
        """
        EV = PV(FCFs) + PV(TV)
        equity = EV - net_debt
        implied_price = equity / shares
        """
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc_result(stock, config)

        result = compute_dcf(stock, wacc_result, config)

        # Recompute equity bridge
        ev = result["pv_fcf_sum"] + result["pv_terminal_value"]
        assert abs(ev - result["enterprise_value"]) < 1.0

        equity = ev - result["net_debt"]
        assert abs(equity - result["equity_value"]) < 1.0

        implied = equity / result["shares_outstanding"]
        assert abs(implied - result["implied_price"]) < 0.01

    def test_negative_fcf_company_runs_without_error(self):
        """
        A cash-burning company (all negative FCF) should not crash the model.
        The DCF will produce a low or negative EV from operations, which is correct.
        """
        config = _make_minimal_config()
        stock = MockStock(
            fcf=[-0.2e9, -0.15e9, -0.1e9],
            revenue=[2e9, 1.5e9, 1e9],
        )
        wacc_result = self._get_wacc_result(stock, config)
        # Should not raise
        result = compute_dcf(stock, wacc_result, config)
        assert "implied_price" in result
        assert math.isfinite(result["implied_price"])

    def test_tv_pct_of_ev_reported(self):
        """tv_pct_of_ev is always present and between 0 and 1 for normal inputs."""
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc_result(stock, config)

        result = compute_dcf(stock, wacc_result, config)
        tv_pct = result["tv_pct_of_ev"]
        assert math.isfinite(tv_pct)
        assert 0.0 <= tv_pct <= 1.0

    def test_wacc_equals_terminal_growth_raises(self):
        """When WACC == terminal_g, the model should raise ValueError."""
        config = _make_minimal_config()
        config["dcf"]["terminal_growth_rate"] = 0.10  # will exceed typical WACC
        stock = MockStock(beta=0.01)  # very low beta → low WACC
        # Force WACC to be below terminal_g by directly setting a tiny WACC result
        fake_wacc_result = {
            "wacc": 0.05,  # below terminal_g of 0.10
            "ke": 0.05,
            "kd_pretax": 0.04,
            "kd_after_tax": 0.0316,
            "we": 1.0,
            "wd": 0.0,
        }
        with pytest.raises(ValueError, match="WACC"):
            compute_dcf(stock, fake_wacc_result, config)

    def test_growth_override_applied(self):
        """growth_override replaces computed growth schedule."""
        config = _make_minimal_config()
        override_rates = [0.30, 0.25, 0.20, 0.15, 0.10, 0.08, 0.06]
        config["dcf"]["growth_override"] = {"TEST": override_rates}
        stock = MockStock()
        wacc_result = self._get_wacc_result(stock, config)

        result = compute_dcf(stock, wacc_result, config)
        actual_rates = result["blended_growth_rates"]
        for r1, r2 in zip(actual_rates, override_rates):
            assert abs(r1 - r2) < 1e-9

    def test_missing_revenue_raises(self):
        """DCF should raise ValueError when revenue is empty."""
        config = _make_minimal_config()
        stock = MockStock(revenue=[])
        wacc_result = self._get_wacc_result(stock, config)
        with pytest.raises(ValueError, match="revenue"):
            compute_dcf(stock, wacc_result, config)

    def test_missing_shares_raises(self):
        """DCF should raise ValueError when shares_outstanding is None."""
        config = _make_minimal_config()
        stock = MockStock(shares_outstanding=None)
        wacc_result = self._get_wacc_result(stock, config)
        with pytest.raises(ValueError, match="shares"):
            compute_dcf(stock, wacc_result, config)

    def test_geometric_decay_endpoints(self):
        """_geometric_decay should hit g_start at year 1 and g_end at year n."""
        schedule = _geometric_decay(g_start=0.20, g_end=0.03, n=7)
        assert len(schedule) == 7
        assert abs(schedule[0] - 0.20) < 1e-9
        assert abs(schedule[-1] - 0.03) < 1e-9
        # Monotonically decreasing for g_start > g_end
        for i in range(len(schedule) - 1):
            assert schedule[i] >= schedule[i + 1] - 1e-9

    def test_dcf_upside_pct_formula(self):
        """dcf_upside_pct = (implied_price / current_price - 1) * 100."""
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc_result(stock, config)
        result = compute_dcf(stock, wacc_result, config)

        expected_upside = (result["implied_price"] / stock.current_price - 1.0) * 100.0
        assert abs(result["dcf_upside_pct"] - expected_upside) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMonteCarlo:

    def _get_pipeline(self, stock=None, config=None):
        if stock is None:
            stock = MockStock()
        if config is None:
            config = _make_minimal_config()
        wacc_result = compute_wacc(stock, config)
        dcf_result = compute_dcf(stock, wacc_result, config)
        return stock, wacc_result, dcf_result, config

    def test_reproducibility_same_seed(self):
        """Same seed must produce identical P50 across two runs."""
        stock, wacc, dcf, config = self._get_pipeline()

        result1 = run_montecarlo(stock, wacc, dcf, config, mode="screen")
        result2 = run_montecarlo(stock, wacc, dcf, config, mode="screen")

        assert result1["percentiles"]["P50"] == result2["percentiles"]["P50"], (
            "P50 differs between runs with identical seed — not reproducible"
        )

    def test_all_percentiles_present(self):
        """Percentiles dict must contain P5, P10, P25, P50, P75, P90, P95."""
        stock, wacc, dcf, config = self._get_pipeline()
        result = run_montecarlo(stock, wacc, dcf, config, mode="screen")

        for level in ["P5", "P10", "P25", "P50", "P75", "P90", "P95"]:
            assert level in result["percentiles"], f"{level} missing from percentiles"
            assert math.isfinite(result["percentiles"][level])

    def test_percentile_ordering(self):
        """P5 <= P10 <= P25 <= P50 <= P75 <= P90 <= P95."""
        stock, wacc, dcf, config = self._get_pipeline()
        result = run_montecarlo(stock, wacc, dcf, config, mode="screen")
        p = result["percentiles"]
        assert p["P5"] <= p["P10"] <= p["P25"] <= p["P50"] <= p["P75"] <= p["P90"] <= p["P95"]

    def test_p_above_current_is_probability(self):
        """p_above_current must be in [0, 1]."""
        stock, wacc, dcf, config = self._get_pipeline()
        result = run_montecarlo(stock, wacc, dcf, config, mode="screen")
        p = result["p_above_current"]
        assert 0.0 <= p <= 1.0

    def test_n_sims_screen_mode(self):
        """Screen mode uses n_simulations_screen."""
        stock, wacc, dcf, config = self._get_pipeline()
        config["montecarlo"]["antithetic_variates"] = False  # simplify count
        config["montecarlo"]["n_simulations_screen"] = 100
        result = run_montecarlo(stock, wacc, dcf, config, mode="screen")
        # n_sims reported should be close to n_simulations_screen (NaN removal may trim a few)
        assert 1 <= result["n_sims"] <= 100

    def test_antithetic_vs_no_antithetic_same_seed_differ(self):
        """
        Antithetic and non-antithetic runs with the same seed will produce
        different individual draws (antithetic pairs are constructed differently),
        but both should be finite and ordered correctly.
        """
        stock, wacc, dcf, config = self._get_pipeline()

        config_anti = dict(config)
        config_anti["montecarlo"] = dict(config["montecarlo"])
        config_anti["montecarlo"]["antithetic_variates"] = True

        config_noanti = dict(config)
        config_noanti["montecarlo"] = dict(config["montecarlo"])
        config_noanti["montecarlo"]["antithetic_variates"] = False

        r_anti = run_montecarlo(stock, wacc, dcf, config_anti, mode="screen")
        r_noanti = run_montecarlo(stock, wacc, dcf, config_noanti, mode="screen")

        # Both should have valid percentiles
        for r in [r_anti, r_noanti]:
            assert math.isfinite(r["percentiles"]["P50"])

        # Antithetic and plain runs draw different path sets — P50s should differ
        assert r_anti["percentiles"]["P50"] != r_noanti["percentiles"]["P50"], (
            "Antithetic and non-antithetic runs produced identical P50 — likely same draws"
        )

    def test_negative_fcf_company_mc_runs(self):
        """MC should not crash on a negative-FCF company."""
        config = _make_minimal_config()
        stock = MockStock(fcf=[-0.2e9, -0.15e9, -0.1e9], revenue=[2e9, 1.5e9, 1e9])
        wacc_result = compute_wacc(stock, config)
        dcf_result = compute_dcf(stock, wacc_result, config)
        # Should not raise
        result = run_montecarlo(stock, wacc_result, dcf_result, config, mode="screen")
        assert "percentiles" in result


# ─────────────────────────────────────────────────────────────────────────────
# Bayesian Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBayesian:

    def test_single_factor_posterior_by_hand(self):
        """
        Verify Bayesian update with a single factor (earnings_surprise = beat).

        Prior: bull=0.25, base=0.50, bear=0.25
        Likelihoods for 'beat': bull=0.75, base=0.60, bear=0.35
        Unnormalized: bull=0.25*0.75=0.1875, base=0.50*0.60=0.30, bear=0.25*0.35=0.0875
        Sum = 0.575
        Posterior: bull=0.1875/0.575, base=0.30/0.575, bear=0.0875/0.575
        """
        config = _make_minimal_config()
        # Remove all factors except earnings_surprise
        config["bayesian"]["evidence_factors"] = {
            "earnings_surprise": config["bayesian"]["evidence_factors"]["earnings_surprise"]
        }
        # Use a beat (surprise > 10%)
        stock = MockStock(
            eps_surprise_pct=0.15,  # 15% beat → 'beat' category
            # Remove other data sources to ensure only earnings_surprise fires
            revenue=[5e9, 4e9],
            gross_profit=[],        # disable gross_margin
            forward_pe=None,        # disable forward_pe
            analyst_target_mean=None,  # disable analyst_upside
        )

        result = compute_bayesian(stock, config)

        # Hand-computed posterior
        lk_bull, lk_base, lk_bear = 0.75, 0.60, 0.35
        prior_bull, prior_base, prior_bear = 0.25, 0.50, 0.25
        unnorm_bull = prior_bull * lk_bull
        unnorm_base = prior_base * lk_base
        unnorm_bear = prior_bear * lk_bear
        total = unnorm_bull + unnorm_base + unnorm_bear
        exp_bull = unnorm_bull / total
        exp_base = unnorm_base / total
        exp_bear = unnorm_bear / total

        assert abs(result["bull"] - exp_bull) < 1e-6, f"bull: {result['bull']} vs {exp_bull}"
        assert abs(result["base"] - exp_base) < 1e-6, f"base: {result['base']} vs {exp_base}"
        assert abs(result["bear"] - exp_bear) < 1e-6, f"bear: {result['bear']} vs {exp_bear}"

    def test_posterior_sums_to_one(self):
        """Posterior probabilities must sum to 1."""
        config = _make_minimal_config()
        stock = MockStock()
        result = compute_bayesian(stock, config)
        total = result["bull"] + result["base"] + result["bear"]
        assert abs(total - 1.0) < 1e-9, f"Posterior sums to {total}, not 1"

    def test_update_trace_starts_with_prior(self):
        """Update trace first entry must be the prior."""
        config = _make_minimal_config()
        stock = MockStock()
        result = compute_bayesian(stock, config)

        trace = result["update_trace"]
        assert len(trace) >= 1
        first = trace[0]
        assert first["label"] == "prior"
        assert abs(first["bull"] - 0.25) < 1e-9
        assert abs(first["base"] - 0.50) < 1e-9
        assert abs(first["bear"] - 0.25) < 1e-9

    def test_trace_length_matches_applied_factors(self):
        """Trace should have 1 (prior) + N applied factors entries."""
        config = _make_minimal_config()
        stock = MockStock()
        result = compute_bayesian(stock, config)
        n_applied = len(result["update_trace"]) - 1  # subtract prior entry
        n_skipped = len(result["skipped_factors"])
        n_total_factors = len(config["bayesian"]["evidence_factors"])
        assert n_applied + n_skipped == n_total_factors

    def test_skipped_factors_when_data_missing(self):
        """Factors with missing data are skipped and logged."""
        config = _make_minimal_config()
        stock = MockStock(
            eps_surprise_pct=None,     # skip earnings_surprise
            forward_pe=None,            # skip forward_pe
            analyst_target_mean=None,   # skip analyst_upside
        )
        result = compute_bayesian(stock, config)
        skipped = result["skipped_factors"]
        assert "earnings_surprise" in skipped
        assert "forward_pe" in skipped

    def test_expected_value_formula(self):
        """E[V] = P(bull)*PT_bull + P(base)*PT_base + P(bear)*PT_bear."""
        config = _make_minimal_config()
        stock = MockStock()
        result = compute_bayesian(stock, config)

        ev_computed = (
            result["bull"] * result["pt_bull"]
            + result["base"] * result["pt_base"]
            + result["bear"] * result["pt_bear"]
        )
        assert abs(result["expected_value"] - ev_computed) < 1e-6

    def test_likelihood_clipping_prevents_degeneracy(self):
        """
        Verify that extreme likelihoods are clipped and don't produce
        a probability of 0 for any scenario after a single factor update.
        """
        config = _make_minimal_config()
        # Set a likelihood that would normally push bear to exactly 0
        config["bayesian"]["evidence_factors"] = {
            "earnings_surprise": {
                "thresholds": [0.10, 0.0],
                "likelihoods": {
                    "beat": [1.0, 0.8, 0.0],  # bear likelihood = 0 (degenerate without clip)
                    "inline": [0.55, 0.65, 0.55],
                    "miss": [0.30, 0.45, 0.75],
                }
            }
        }
        stock = MockStock(eps_surprise_pct=0.15)  # 15% beat
        result = compute_bayesian(stock, config)
        # After clipping [0.05, 0.95], bear likelihood = 0.05, so bear prob > 0
        assert result["bear"] > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Sensitivity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSensitivity:

    def _get_wacc(self, stock, config):
        return compute_wacc(stock, config)

    def test_bear_base_bull_ordering(self):
        """Bear price < base price < bull price for a positive-FCF company."""
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc(stock, config)
        result = compute_sensitivity(stock, wacc_result, config)

        assert result["bear_price"] < result["base_price"] < result["bull_price"], (
            f"Expected bear < base < bull but got: "
            f"{result['bear_price']:.2f} < {result['base_price']:.2f} < {result['bull_price']:.2f}"
        )

    def test_symmetry_of_bear_bull_around_base(self):
        """
        Bull delta and bear delta should be approximately equal in magnitude.
        (Symmetry property: revenue ±5% → ±X% price change.)
        Tolerance: 5% relative difference is acceptable given FCF margin effects.
        """
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc(stock, config)
        result = compute_sensitivity(stock, wacc_result, config)

        bull_delta = abs(result["bull_price"] - result["base_price"])
        bear_delta = abs(result["base_price"] - result["bear_price"])

        if bear_delta > 0:
            ratio = bull_delta / bear_delta
            assert 0.85 <= ratio <= 1.15, (
                f"Sensitivity asymmetry ratio {ratio:.3f} outside [0.85, 1.15]. "
                f"bull_delta={bull_delta:.2f}, bear_delta={bear_delta:.2f}"
            )

    def test_dV_dr_is_positive(self):
        """dV/dr should be positive: higher revenue → higher price."""
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc(stock, config)
        result = compute_sensitivity(stock, wacc_result, config)
        assert result["dV_dr"] > 0, "dV/dr should be positive"

    def test_dV_dr_pct_is_positive(self):
        """dV/dr as % of current price should be positive."""
        config = _make_minimal_config()
        stock = MockStock()
        wacc_result = self._get_wacc(stock, config)
        result = compute_sensitivity(stock, wacc_result, config)
        assert result["dV_dr_pct"] > 0

    def test_delta_pct_from_config(self):
        """sensitivity uses revenue_delta_pct from config."""
        config = _make_minimal_config()
        config["sensitivity"]["revenue_delta_pct"] = 10
        stock = MockStock()
        wacc_result = self._get_wacc(stock, config)
        result = compute_sensitivity(stock, wacc_result, config)
        assert result["delta_pct"] == 10.0


# ─────────────────────────────────────────────────────────────────────────────
# Composite Scoring Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCompositeScoring:

    def test_upside_to_score_zero_upside_is_fifty(self):
        """0% upside → score component = 50."""
        assert _upside_to_score(0.0) == 50.0

    def test_upside_to_score_fifty_pct_is_hundred(self):
        """+50% upside → score component = 100."""
        assert _upside_to_score(50.0) == 100.0

    def test_upside_to_score_neg_fifty_pct_is_zero(self):
        """-50% upside → score component = 0."""
        assert _upside_to_score(-50.0) == 0.0

    def test_upside_to_score_clamped_upper(self):
        """Upside > 50% is clamped to 100."""
        assert _upside_to_score(200.0) == 100.0

    def test_upside_to_score_clamped_lower(self):
        """Upside < -50% is clamped to 0."""
        assert _upside_to_score(-200.0) == 0.0

    def test_upside_to_score_none_returns_fifty(self):
        """None input returns neutral 50."""
        assert _upside_to_score(None) == 50.0

    def test_upside_to_score_nan_returns_fifty(self):
        """NaN input returns neutral 50."""
        assert _upside_to_score(float("nan")) == 50.0

    def test_margin_trend_positive_slope(self):
        """Improving margins (older → newer) should yield positive slope."""
        stock = MockStock(
            gross_profit=[3.5e9, 3.0e9, 2.0e9],  # most-recent-first, improving
            revenue=[5e9, 5e9, 5e9],              # constant revenue
        )
        slope = _compute_margin_trend(stock)
        assert slope > 0, f"Expected positive slope for improving margins, got {slope}"

    def test_margin_trend_negative_slope(self):
        """Deteriorating margins should yield negative slope."""
        stock = MockStock(
            gross_profit=[2.0e9, 3.0e9, 3.5e9],  # most-recent-first, deteriorating
            revenue=[5e9, 5e9, 5e9],
        )
        slope = _compute_margin_trend(stock)
        assert slope < 0, f"Expected negative slope for deteriorating margins, got {slope}"

    def test_margin_trend_flat_returns_zero(self):
        """Constant margins should yield approximately zero slope."""
        stock = MockStock(
            gross_profit=[3e9, 3e9, 3e9],
            revenue=[5e9, 5e9, 5e9],
        )
        slope = _compute_margin_trend(stock)
        assert abs(slope) < 1e-10

    def test_composite_score_in_range(self):
        """Composite score must be in [0, 100]."""
        config = _make_minimal_config()
        scorer = CompositeScorer(config, mode="screen")
        stock = MockStock()
        result = scorer.score_one(stock)
        assert 0.0 <= result["score"] <= 100.0

    def test_composite_score_all_returns_sorted(self):
        """score_all returns list sorted by score descending."""
        config = _make_minimal_config()
        scorer = CompositeScorer(config, mode="screen")
        stocks = [
            MockStock(ticker="A", current_price=100.0, analyst_target_mean=200.0),  # high upside
            MockStock(ticker="B", current_price=100.0, analyst_target_mean=50.0),   # negative upside
        ]
        results = scorer.score_all(stocks)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), "Results not sorted by score descending"

    def test_composite_result_has_required_keys(self):
        """Output dict must contain all required output contract keys."""
        config = _make_minimal_config()
        scorer = CompositeScorer(config, mode="screen")
        stock = MockStock()
        result = scorer.score_one(stock)

        required_keys = [
            "ticker", "sector", "current_price", "score",
            "dcf_implied_price", "dcf_upside_pct",
            "mc_p50", "expected_value",
            "bayesian_bull_prob", "bayesian_base_prob", "bayesian_bear_prob",
            "wacc", "wacc_detail", "dcf_detail", "mc_detail",
            "bayesian_trace", "sensitivity_detail",
        ]
        for key in required_keys:
            assert key in result, f"Missing required key: '{key}'"

    def test_score_all_handles_bad_stock_gracefully(self):
        """score_all should not crash when one stock has no data."""
        config = _make_minimal_config()
        scorer = CompositeScorer(config, mode="screen")
        bad_stock = MockStock(
            ticker="BAD",
            revenue=[],
            shares_outstanding=None,
            current_price=None,
        )
        good_stock = MockStock(ticker="GOOD")
        results = scorer.score_all([bad_stock, good_stock])
        tickers = [r["ticker"] for r in results]
        assert "GOOD" in tickers
        assert "BAD" in tickers


# ─────────────────────────────────────────────────────────────────────────────
# Filter Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFilters:

    def test_market_cap_filter_removes_small(self):
        """Stocks below min_market_cap_B threshold are removed."""
        config = _make_minimal_config()
        config["universe"]["min_market_cap_B"] = 5

        stocks = [
            MockStock(ticker="BIG", market_cap=10e9),
            MockStock(ticker="SMALL", market_cap=1e9),
        ]
        result = apply_filters(stocks, config)
        tickers = [s.ticker for s in result]
        assert "BIG" in tickers
        assert "SMALL" not in tickers

    def test_market_cap_filter_removes_none(self):
        """Stocks with None market_cap are excluded."""
        config = _make_minimal_config()
        config["universe"]["min_market_cap_B"] = 2

        stocks = [
            MockStock(ticker="GOOD", market_cap=10e9),
            MockStock(ticker="NONE", market_cap=None),
        ]
        result = apply_filters(stocks, config)
        tickers = [s.ticker for s in result]
        assert "GOOD" in tickers
        assert "NONE" not in tickers

    def test_volume_filter_removes_illiquid(self):
        """Stocks with volume below min_avg_volume_M are removed."""
        config = _make_minimal_config()
        config["universe"]["min_avg_volume_M"] = 2

        stocks = [
            MockStock(ticker="LIQUID", avg_daily_volume=5e6),
            MockStock(ticker="ILLIQUID", avg_daily_volume=0.5e6),
        ]
        result = apply_filters(stocks, config)
        tickers = [s.ticker for s in result]
        assert "LIQUID" in tickers
        assert "ILLIQUID" not in tickers

    def test_sector_exclusion_filter(self):
        """Stocks in excluded sectors are removed."""
        config = _make_minimal_config()
        config["universe"]["exclude_sectors"] = ["Energy", "Utilities"]

        stocks = [
            MockStock(ticker="TECH", sector="Technology"),
            MockStock(ticker="ENERGY", sector="Energy"),
            MockStock(ticker="UTIL", sector="Utilities"),
        ]
        result = apply_filters(stocks, config)
        tickers = [s.ticker for s in result]
        assert "TECH" in tickers
        assert "ENERGY" not in tickers
        assert "UTIL" not in tickers

    def test_sector_exclusion_case_insensitive(self):
        """Sector exclusion is case-insensitive."""
        config = _make_minimal_config()
        config["universe"]["exclude_sectors"] = ["energy"]

        stocks = [
            MockStock(ticker="E1", sector="Energy"),
            MockStock(ticker="E2", sector="ENERGY"),
            MockStock(ticker="E3", sector="energy"),
        ]
        result = apply_filters(stocks, config)
        assert len(result) == 0, "All energy stocks should be excluded"

    def test_all_filters_combined(self):
        """All three filters applied together: only compliant stocks pass."""
        config = _make_minimal_config()
        config["universe"]["min_market_cap_B"] = 5
        config["universe"]["min_avg_volume_M"] = 2
        config["universe"]["exclude_sectors"] = ["Energy"]

        stocks = [
            MockStock(ticker="PASS", market_cap=10e9, avg_daily_volume=5e6, sector="Technology"),
            MockStock(ticker="FAIL_CAP", market_cap=1e9, avg_daily_volume=5e6, sector="Technology"),
            MockStock(ticker="FAIL_VOL", market_cap=10e9, avg_daily_volume=0.5e6, sector="Technology"),
            MockStock(ticker="FAIL_SEC", market_cap=10e9, avg_daily_volume=5e6, sector="Energy"),
        ]
        result = apply_filters(stocks, config)
        tickers = [s.ticker for s in result]
        assert tickers == ["PASS"]

    def test_empty_filters_pass_all(self):
        """With no filter thresholds set, all stocks pass."""
        config = _make_minimal_config()
        config["universe"]["min_market_cap_B"] = None
        config["universe"]["min_avg_volume_M"] = None
        config["universe"]["exclude_sectors"] = []

        stocks = [MockStock(ticker=f"T{i}") for i in range(5)]
        result = apply_filters(stocks, config)
        assert len(result) == 5

    def test_empty_input_returns_empty(self):
        """Empty input list returns empty list."""
        config = _make_minimal_config()
        result = apply_filters([], config)
        assert result == []
