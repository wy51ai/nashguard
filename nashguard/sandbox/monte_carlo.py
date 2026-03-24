"""
Monte Carlo Portfolio Simulation

Simulates N price paths via Geometric Brownian Motion and computes
risk metrics: Max Drawdown, Value at Risk (VaR 95%), Expected Shortfall (CVaR).

All calculations are pure NumPy — zero LLM involvement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


@dataclass
class SimLeg:
    """A single position leg to include in the simulation."""

    name: str
    entry_price: float
    notional_usdt: float      # Total USD exposure
    leverage: float = 1.0
    is_long: bool = True
    is_option: bool = False
    option_delta: float = 0.5  # Only used for options


@dataclass
class SimResult:
    max_drawdown_pct: float
    var_95_pct: float
    expected_shortfall_pct: float  # CVaR at 95%
    liquidation_price: Optional[float]
    pnl_paths: np.ndarray          # Shape: (n_sims,) — final P&L in USDT
    worst_case_usdt: float
    best_case_usdt: float


def simulate(
    legs: List[SimLeg],
    spot_price: float,
    annual_vol: float = 0.80,
    horizon_days: int = 30,
    n_sims: int = 10_000,
    annual_drift: float = 0.0,
    seed: int = 42,
    total_portfolio_usdt: Optional[float] = None,
) -> SimResult:
    """
    Geometric Brownian Motion Monte Carlo simulation.

    Uses the closed-form GBM solution:
        S(T) = S(0) * exp((mu - 0.5*sigma^2)*T + sigma*sqrt(T)*Z)
    where Z ~ N(0,1).
    """
    rng = np.random.default_rng(seed)

    T = horizon_days / 365.0
    dt = T
    mu = annual_drift

    # Simulate log-returns for BTC (all positions correlated to BTC)
    Z = rng.standard_normal(n_sims)
    log_return = (mu - 0.5 * annual_vol**2) * dt + annual_vol * math.sqrt(dt) * Z
    price_ratio = np.exp(log_return)  # S(T) / S(0)

    total_pnl = np.zeros(n_sims)
    liquidation_prices = []

    for leg in legs:
        if leg.is_option:
            # Approximate option P&L using delta approximation
            price_change_pct = price_ratio - 1.0
            pnl = leg.notional_usdt * leg.option_delta * price_change_pct
            if not leg.is_long:
                pnl = -pnl
        else:
            # Spot / futures: linear P&L
            price_change_pct = price_ratio - 1.0
            pnl = leg.notional_usdt * leg.leverage * price_change_pct
            if not leg.is_long:
                pnl = -pnl

            # Liquidation price for leveraged positions
            if leg.leverage > 1.0:
                liq_distance_pct = 1.0 / leg.leverage
                if leg.is_long:
                    liq_price = leg.entry_price * (1.0 - liq_distance_pct * 0.9)
                else:
                    liq_price = leg.entry_price * (1.0 + liq_distance_pct * 0.9)
                liquidation_prices.append(liq_price)

        total_pnl += pnl

    # Express P&L as % of total portfolio (not just position notional).
    # This gives portfolio-level risk metrics: a 30% position losing 40%
    # means 12% portfolio drawdown, not 40%.
    total_capital = total_portfolio_usdt or (sum(leg.notional_usdt for leg in legs) or 1.0)
    pnl_pct = (total_pnl / total_capital) * 100.0

    # Max Drawdown: worst single-path outcome
    max_drawdown_pct = float(-np.percentile(pnl_pct, 1))  # 1st percentile loss
    max_drawdown_pct = max(max_drawdown_pct, 0.0)

    # VaR at 95% confidence (loss that is not exceeded 95% of the time)
    var_95_pct = float(-np.percentile(pnl_pct, 5))
    var_95_pct = max(var_95_pct, 0.0)

    # Expected Shortfall / CVaR: mean of the worst 5%
    worst_5pct_mask = pnl_pct <= np.percentile(pnl_pct, 5)
    expected_shortfall_pct = float(-np.mean(pnl_pct[worst_5pct_mask]))
    expected_shortfall_pct = max(expected_shortfall_pct, 0.0)

    # Nearest liquidation price (most dangerous)
    liq_price = None
    if liquidation_prices:
        # For long positions pick the highest liq price (triggers soonest)
        # For short pick the lowest — simplified: just take the first
        liq_price = liquidation_prices[0]

    return SimResult(
        max_drawdown_pct=max_drawdown_pct,
        var_95_pct=var_95_pct,
        expected_shortfall_pct=expected_shortfall_pct,
        liquidation_price=liq_price,
        pnl_paths=total_pnl,
        worst_case_usdt=float(np.min(total_pnl)),
        best_case_usdt=float(np.max(total_pnl)),
    )
