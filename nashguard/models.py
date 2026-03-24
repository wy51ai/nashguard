"""
NashGuard Data Models

All structured types that flow through the MoA debate pipeline and DRB sandbox.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Strategy(str, Enum):
    SPOT_BUY = "spot_buy"
    SPOT_SELL = "spot_sell"
    FUTURES_LONG = "futures_long"
    FUTURES_SHORT = "futures_short"
    COVERED_CALL = "covered_call"
    PROTECTIVE_PUT = "protective_put"
    CALL_BUY = "call_buy"
    PUT_BUY = "put_buy"
    COMBINED = "combined"
    HOLD = "hold"


class Leg(BaseModel):
    """One instrument leg in a trade proposal."""

    module: str = Field(description="OKX module: spot | swap | option")
    instId: str = Field(description="Instrument ID, e.g. BTC-USDT or BTC-USD-240329-50000-C")
    side: str = Field(description="buy | sell")
    size_pct: float = Field(
        ge=0.0, le=100.0, description="Percentage of available balance to allocate"
    )
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    ordType: str = Field(default="market", description="market | limit")
    price: Optional[float] = Field(default=None, description="Limit price (omit for market)")
    tp_pct: Optional[float] = Field(default=None, description="Take-profit % above/below entry")
    sl_pct: Optional[float] = Field(default=None, description="Stop-loss % above/below entry")


class OKXToolCall(BaseModel):
    """A specific OKX Agent Trade Kit tool call to execute."""

    tool: str
    arguments: Dict[str, Any]


class TradeProposal(BaseModel):
    """Alpha agent's proposed trading strategy."""

    strategy: Strategy
    legs: List[Leg]
    rationale: str
    expected_return_pct: float
    risk_summary: str
    tool_calls: List[OKXToolCall] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    """Risk agent's assessment of a trade proposal."""

    risk_approved: bool
    risk_score: float = Field(ge=0.0, le=10.0, description="0=safe, 10=catastrophic")
    violations: List[str] = Field(default_factory=list)
    rationale: str
    suggested_adjustments: Optional[str] = None


class DRBResult(BaseModel):
    """Output from the Deterministic Risk Bounding sandbox."""

    max_drawdown_pct: float
    liquidation_price: Optional[float] = None
    var_95_pct: float = Field(description="Value at Risk at 95% confidence level")
    expected_shortfall_pct: float = Field(description="CVaR / Expected Shortfall")
    approved: bool
    signature_hex: str = Field(description="Ed25519 signature of the risk result payload")
    rejection_reason: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class JudgeDecision(BaseModel):
    """Judge agent's final arbitration decision."""

    approved: bool
    final_strategy: Optional[TradeProposal] = None
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    user_message: str = Field(description="Human-readable message to show the user")


class MarketSnapshot(BaseModel):
    """Live market data fetched from OKX."""

    btc_price: float
    btc_24h_change_pct: float
    btc_funding_rate: Optional[float] = None
    eth_price: Optional[float] = None
    instruments: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str = ""


class AccountSnapshot(BaseModel):
    """Account state fetched from OKX."""

    total_eq_usdt: float
    available_eq_usdt: float
    positions: List[Dict[str, Any]] = Field(default_factory=list)
    has_credentials: bool = True


class NashGuardResult(BaseModel):
    """Complete audit record of a NashGuard decision."""

    user_intent: str
    debate_rounds: int = 0
    alpha_proposal: Optional[TradeProposal] = None
    risk_assessment: Optional[RiskAssessment] = None
    judge_decision: Optional[JudgeDecision] = None
    drb_result: Optional[DRBResult] = None
    execution_results: List[Dict[str, Any]] = Field(default_factory=list)
    rejected: bool = False
    rejection_reason: Optional[str] = None
    audit_log: List[str] = Field(default_factory=list)
    demo_mode: bool = True
