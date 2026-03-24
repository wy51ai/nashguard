"""
Black-Scholes Option Pricing Model

Deterministic, LLM-free calculation of option fair values, Greeks, and
implied volatility. Used by the DRB sandbox for accurate risk estimation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from scipy.stats import norm


@dataclass
class BSInputs:
    S: float        # Spot price
    K: float        # Strike price
    T: float        # Time to expiry (years)
    r: float        # Annual risk-free rate (e.g. 0.05)
    sigma: float    # Annualised implied volatility (e.g. 0.80)
    option_type: Literal["call", "put"] = "call"


@dataclass
class BSResult:
    price: float
    delta: float
    gamma: float
    theta: float    # Per day
    vega: float     # Per 1% IV move
    rho: float
    d1: float
    d2: float


def price(inputs: BSInputs) -> BSResult:
    """Compute Black-Scholes price and Greeks for a European option."""
    S, K, T, r, sigma = inputs.S, inputs.K, inputs.T, inputs.r, inputs.sigma

    if T <= 0:
        intrinsic = max(S - K, 0) if inputs.option_type == "call" else max(K - S, 0)
        return BSResult(
            price=intrinsic, delta=1.0 if S > K else 0.0,
            gamma=0.0, theta=0.0, vega=0.0, rho=0.0, d1=0.0, d2=0.0,
        )

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    Nd1 = norm.cdf(d1)
    Nd2 = norm.cdf(d2)
    nd1 = norm.pdf(d1)
    disc = math.exp(-r * T)

    if inputs.option_type == "call":
        option_price = S * Nd1 - K * disc * Nd2
        delta = Nd1
        rho = K * T * disc * Nd2 / 100
    else:
        option_price = K * disc * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = Nd1 - 1
        rho = -K * T * disc * norm.cdf(-d2) / 100

    gamma = nd1 / (S * sigma * sqrt_T)
    theta = (
        -(S * nd1 * sigma) / (2 * sqrt_T)
        - r * K * disc * (Nd2 if inputs.option_type == "call" else norm.cdf(-d2))
    ) / 365
    vega = S * nd1 * sqrt_T / 100

    return BSResult(
        price=option_price,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        d1=d1,
        d2=d2,
    )


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: Literal["call", "put"] = "call",
    max_iter: int = 100,
    tol: float = 1e-6,
) -> float:
    """Newton-Raphson solver for implied volatility."""
    if T <= 0:
        return float("nan")

    sigma = 0.5  # initial guess
    for _ in range(max_iter):
        inputs = BSInputs(S=S, K=K, T=T, r=r, sigma=sigma, option_type=option_type)
        result = price(inputs)
        diff = result.price - market_price
        if abs(diff) < tol:
            return sigma
        vega_raw = result.vega * 100  # undo the /100 scaling
        if abs(vega_raw) < 1e-10:
            break
        sigma -= diff / vega_raw
        sigma = max(0.001, min(sigma, 10.0))

    return sigma


def max_loss_option_buyer(premium_paid: float, contracts: float = 1) -> float:
    """Maximum loss for an option buyer is the total premium paid."""
    return premium_paid * contracts


def max_loss_covered_call(
    spot_entry: float,
    strike: float,
    premium_received: float,
) -> float:
    """
    Maximum loss for a covered call position.
    Worst case: spot drops to zero. Net cost = entry - premium received.
    """
    return spot_entry - premium_received
