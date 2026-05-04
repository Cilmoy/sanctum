import pytest
from rich.console import Group
from sanctum.output.terminal import TerminalOutput

@pytest.fixture
def sample_result():
    return {
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "current_price": 175.0,
        "score": 82.5,
        "catalyst_score": 78.0,
        "trade_archetype": "CONVERGENCE",
        "dcf_implied_price": 210.0,
        "dcf_upside_pct": 20.0,
        "mc_p50": 205.0,
        "expected_value": 208.0,
        "bayesian_bull_prob": 0.6,
        "bayesian_base_prob": 0.3,
        "bayesian_bear_prob": 0.1,
        "score_components": {
            "bayesian_upside": 85.0,
            "mc_upside": 80.0,
            "dcf_upside": 75.0,
            "earnings_momentum": 90.0,
            "sentiment_score": 70.0,
            "margin_trend": 88.0,
        },
        "catalyst_detail": {
            "components": {
                "earnings_acceleration": 85.0,
                "smart_money": 70.0,
                "analyst_revisions": 90.0,
                "price_momentum": 80.0,
                "short_interest": 50.0,
            },
            "notes": ["Positive earnings momentum", "Strong insider alignment"]
        },
        "wacc_detail": {"wacc": 0.08, "beta": 1.1, "ke": 0.09, "kd_after_tax": 0.04, "we": 0.8, "wd": 0.2},
        "dcf_detail": {
            "projection_rows": [
                {"year": 1, "revenue": 400e9, "fcf_margin": 0.25, "fcf": 100e9, "pv_fcf": 92e9}
            ],
            "terminal_value": 3000e9,
            "pv_terminal_value": 1500e9,
            "enterprise_value": 2500e9,
            "net_debt": 50e9,
            "implied_price": 210.0,
            "notes": ["High conviction"]
        },
        "options_analysis": {
            "expiration": "2024-06-21",
            "dte": 30,
            "atm_iv": 0.25,
            "iv_regime": "low",
            "strategy": {"name": "Bull Call Spread", "rationale": "Bullish view, low IV"}
        }
    }

def test_terminal_output_analysis_renderables(sample_result):
    config = {"output": {"brand_name": "SANCTUM TEST"}}
    output = TerminalOutput(config)
    renderable = output.get_analysis_renderables(sample_result, show_math=True)
    
    assert isinstance(renderable, Group)
    # Check that some expected text or components are present
    # Group is an iterable of renderables
    renderables = list(renderable.renderables)
    assert len(renderables) > 0
    
    # Check if any renderable contains the ticker
    ticker_found = False
    for r in renderables:
        if hasattr(r, "plain") and "AAPL" in r.plain:
            ticker_found = True
            break
        if hasattr(r, "title") and "AAPL" in str(r.title):
            ticker_found = True
            break
    # For Text objects in the group
    if not ticker_found:
        for r in renderables:
            if hasattr(r, "__str__") and "AAPL" in str(r):
                ticker_found = True
                break
    
    assert ticker_found

def test_terminal_output_screen_results_table(sample_result):
    config = {"output": {"top_n": 10}}
    output = TerminalOutput(config)
    results = [sample_result]
    shortlisted = [sample_result]
    renderable = output.get_screen_results_table(results, shortlisted)
    
    assert isinstance(renderable, Group)
    assert len(list(renderable.renderables)) == 2 # info Text + Table
