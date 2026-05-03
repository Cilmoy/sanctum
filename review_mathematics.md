# Sanctum Equity Screener — Quantitative & Mathematical Review

**Reviewers:** Ex-Jane Street Quant & PhD Mathematician
**Date:** 2026-05-03
**Focus:** DCF Undervaluation of Monopolies, Options Pricing, and 30%-10x Short-Term Gainers

---

## 1. Why TSM and GOOG are Screaming "Sell" (The Valuation Bias)

### The FCF Margin Cap (The 25% Rule)
**Jane Street Perspective:** The model is effectively shorting the best businesses in the world. In `dcf.py`, `_FCF_MARGIN_CAP` is hardcoded to 25%. If a company prints a 35% margin (like TSM or GOOG), the model forcibly "mean-reverts" it by blending 50% of their historical margin with 50% of a 10% sector fallback. 
*Math Result:* A 35% actual margin becomes normalized to 22.5%. You are instantly vaporizing ~35% of their cash flows forever in the perpetuity. This alone is why your implied prices are broken. High-moat tech monopolies do not mean-revert to a 10% sector average. 

### Aggressive Geometric Decay of Growth
**PhD Perspective:** The `_geometric_decay` function forces growth to decay to 3% over a fixed `n_years` (default 7). A company like GOOG might compound at 10-15% for another decade before structural saturation. Forcing it to 3% in year 7 radically underprices the outer years of the projection. 

### WACC vs. ROIC (The Convexity Mismatch)
**Jane Street Perspective:** Your WACC applies a uniform discount rate to all future flows, but fails to account for the massive Return on Invested Capital (ROIC) these companies generate. A static discount rate applied to artificially compressed margins generates a massive bear bias on structural winners.

**The Fix:**
1. Lift the `_FCF_MARGIN_CAP` to 40-45% for specific sectors (Tech/Semis) or eliminate the sector fallback for companies with >10 years of >20% ROIC.
2. Extend the decay schedule for high-moat companies (e.g., 10-15 years instead of 7).

---

## 2. Options Mathematics & The Black-Scholes Illusion

### The Log-Normal Fallacy Around Earnings
**PhD Perspective:** In `options.py`, `_bs_greeks` uses standard Black-Scholes. BS assumes the underlying asset price follows geometric Brownian motion with continuous paths. This is fundamentally wrong for 1-4 week options held through earnings. Earnings are *jump diffusion* events. The probability density function is bimodal, not log-normal. 

### Volatility Regimes (IV vs HV30)
**Jane Street Perspective:** Your `_iv_regime` function compares ATM Implied Volatility against 30-day Historical Volatility (HV30). If `IV / HV30 > 1.25`, it labels it "high" and suggests selling premium (e.g., Short Iron Condors, Credit Spreads).
*The Reality:* If IV is 1.5x HV30 exactly 2 days before an earnings call, IV isn't "high" in a vacuum—it's pricing in the binary event. Selling premium into that based purely on a DCF conviction score is how you blow up an account. Post-earnings, IV crushes. You are collecting premium but exposing yourself to a jump that exceeds the breakeven of the spread.

**The Fix:**
1. Separate "Catalyst IV" from "Structural IV". You cannot compare pre-earnings IV to trailing HV30. You must compare current IV to *Historical Earnings IV Crush* for that specific ticker.
2. Add a Jump-Diffusion overlay or implied move calculation: `Implied Move ≈ ATM Straddle Price / Current Price`. If your Bayesian/Catalyst model predicts a move larger than the options market implied move, *that* is when you buy premium.

---

## 3. The 30% to 10x Paradigm (4 to 12 Weeks)

You are asking the model to find short-term multi-baggers using a Discounted Cash Flow model. 
**Jane Street Perspective:** This is using a thermometer to measure velocity. DCFs measure 10-year intrinsic value. A stock does not 10x in 4 weeks because its 7th-year cash flow changed. 

If you want 30% to 10x returns in a 4 to 12-week timeframe (or 1-4 weeks with options), you are playing a **liquidity, momentum, and positioning game**, not a value game.

### How to Actually Screen for 10x Potential:

1. **The Gamma Squeeze / Short Squeeze Setup (`catalyst.py`)**
   Your `_score_short_interest` uses short interest as an amplifier. To get 10x options returns, you need forced buying. You need to screen for:
   - High Short Interest (>15%) + High Days to Cover (>5) + Call Skew (high volume of OTM calls forcing market makers to hedge).
   
2. **Earnings Acceleration (The PEAD Trade)**
   Post-Earnings Announcement Drift (PEAD) is real. You need to weight `earnings_beat_accelerating` heavily. A 10x options trade happens when a company reports a fundamental inflection that Wall Street completely mispriced. 
   
3. **The Options Leverage Profile**
   To 10x an option in 1-4 weeks, you must buy OTM calls when IV is artificially *low* (e.g., between earnings cycles) right as a catalyst hits (e.g., a competitor reports blowout earnings, creating sector momentum). Your model currently suggests ATM or 25-delta spreads. Spread strategies cap gains—they will *never* 10x. 

### Recommendations for the Codebase:
1. **Create a "Multi-Bagger" Sub-Screen:** Detach this entirely from the DCF score. Filter strictly by `catalyst_score > 85`. 
2. **Revise Options Strategy for High-Conviction:** If `catalyst_score` is extreme and IV is low, the strategy should recommend **Long OTM Directional Calls** (e.g., 10-delta to 20-delta), not debit spreads. A debit spread structurally caps your max profit.
3. **Fix the DCF Caps:** Stop capping FCF margins at 25% so your base valuations for hyper-growth companies reflect reality.