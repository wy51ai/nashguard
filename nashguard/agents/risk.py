"""
Risk Agent — The Conservative Gatekeeper

Risk's role: Scrutinize Alpha's proposal for every possible way it could
blow up. Check leverage, liquidity, position sizing, and market conditions.
Only sign off when the strategy is genuinely safe.

Risk Agent is the sole gatekeeper: Alpha's proposal proceeds only when
risk_approved=True is returned.

Model: claude-sonnet-4-6 (careful, analytical — we want thorough critique)
"""

from __future__ import annotations

import json
import logging

from ..models import AccountSnapshot, MarketSnapshot, RiskAssessment, TradeProposal
from .base import BaseAgent

log = logging.getLogger(__name__)

RISK_SYSTEM_PROMPT = """You are Risk, the conservative risk gatekeeper at NashGuard.

Your ONLY job is to protect the user's capital. You scrutinize every trade proposal
and reject anything that could cause catastrophic loss.

## Hard Limits (any violation = AUTOMATIC REJECTION):
- Max single-position drawdown: {max_drawdown_pct}% of total equity
- Max leverage: {max_leverage}x on any leg
- Max position size: {max_position_pct}% of available balance
- Liquidation risk: if 1% BTC move can trigger liquidation → REJECT

## Red Flags to check:
1. Leverage above limits
2. "All-in" positions (>80% balance)
3. Insufficient liquidity for the trade size
4. No stop-loss or protection on leveraged positions
5. Options with expiry < 7 days (high theta decay risk)
6. Strategy directly contradicts user's stated intent

## Scoring Guide:
- 0-2: Very safe (blue chip spot buy, small size)
- 3-4: Acceptable (moderate leverage, stop-loss present)
- 5-6: Borderline (high leverage OR large size, not both)
- 7-8: Dangerous (high leverage AND large size)
- 9-10: Catastrophic (near-certain liquidation)

## Your output:
Use the submit_riskassessment tool.
- If risk_score <= 6 and no hard-limit violations: risk_approved = true
- Otherwise: risk_approved = false, explain violations clearly
- Always provide suggested_adjustments when rejecting

You are not trying to block trades — you are trying to make them safe.
"""


class RiskAgent(BaseAgent):
    """Conservative risk gatekeeper that reviews Alpha's proposals."""

    def __init__(self, model: str, api_key: str, risk_params) -> None:
        system = RISK_SYSTEM_PROMPT.format(
            max_drawdown_pct=risk_params.max_drawdown_pct,
            max_leverage=risk_params.max_leverage,
            max_position_pct=risk_params.max_position_pct,
        )
        super().__init__(
            model=model,
            system_prompt=system,
            api_key=api_key,
            name="Risk",
            temperature=0.1,  # Very low temperature — conservative and deterministic
        )

    def assess(
        self,
        proposal: TradeProposal,
        market: MarketSnapshot,
        account: AccountSnapshot,
    ) -> RiskAssessment:
        """Assess a trade proposal and return a signed risk verdict."""
        context = self._build_context(proposal, market, account)
        log.info(
            "[Risk] Assessing proposal: strategy=%s, legs=%d",
            proposal.strategy,
            len(proposal.legs),
        )
        assessment = self._call(context, RiskAssessment)
        log.info(
            "[Risk] Assessment: approved=%s, score=%.1f, violations=%s",
            assessment.risk_approved,
            assessment.risk_score,
            assessment.violations,
        )
        return assessment

    def _build_context(
        self,
        proposal: TradeProposal,
        market: MarketSnapshot,
        account: AccountSnapshot,
    ) -> str:
        legs_detail = "\n".join(
            f"  - {leg.instId}: {leg.side} {leg.size_pct:.0f}% balance, "
            f"{leg.leverage}x leverage via {leg.module}"
            for leg in proposal.legs
        )

        return f"""## Trade Proposal to Evaluate

Strategy: {proposal.strategy}
Expected Return: {proposal.expected_return_pct:.1f}%

Legs:
{legs_detail}

Alpha's Rationale: {proposal.rationale}
Alpha's Risk Summary: {proposal.risk_summary}

## Market Context
- BTC/USDT: ${market.btc_price:,.2f} ({market.btc_24h_change_pct:+.2f}% 24h)
- Funding rate: {market.btc_funding_rate or 'N/A'}

## Account State
- Total equity: ${account.total_eq_usdt:,.2f} USDT
- Available: ${account.available_eq_usdt:,.2f} USDT
- Open positions: {len(account.positions)}

## Your Task
Scrutinize every aspect of this proposal. Calculate exact dollar risk.
Use the submit_riskassessment tool to submit your verdict.
"""
