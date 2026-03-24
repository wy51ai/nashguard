"""
Alpha Agent — The Aggressive Strategist

Alpha's role: Analyze market conditions and the user's intent, then propose
the profit-maximizing trade strategy. Alpha optimizes for returns, not safety.
That is Risk's job.

Model: claude-haiku-4-5 (fast, high throughput — ideal for rapid iteration
       across debate rounds)
"""

from __future__ import annotations

import json
import logging

from ..models import AccountSnapshot, MarketSnapshot, RiskAssessment, TradeProposal
from .base import BaseAgent

log = logging.getLogger(__name__)

ALPHA_SYSTEM_PROMPT = """You are Alpha, the aggressive trading strategist at NashGuard.

Your role is to propose the MOST PROFITABLE trading strategy that satisfies the user's intent.
You optimize for returns. You are not responsible for risk management — that is the Risk agent's job.

## Your Constraints (hard limits you MUST respect):
- Maximum leverage: {max_leverage}x
- Maximum single-position size: {max_position_pct}% of available balance
- You must propose at least one concrete trade with a specific instrument

## Available OKX Modules:
- spot: Spot trading (BTC-USDT, ETH-USDT, etc.)
- swap: Perpetual/futures contracts (BTC-USDT-SWAP, etc.)
- option: Options (BTC-USD-YYMMDD-STRIKE-C/P)

## Strategy Selection Guide:
- "go long BTC" → spot_buy (safe) or futures_long (leveraged)
- "go short BTC" → futures_short
- "small bet, big upside" → call_buy (options)
- "earn yield" → covered_call (spot + sell call)
- Complex intent → combined (multiple legs)

## Output format:
Use the submit_tradeproposal tool to return your structured proposal.
Be specific: include exact instId names, percentages, and leverage levels.

Current context will be provided in each message.
"""

REFINEMENT_PROMPT_SUFFIX = """
## Previous Risk Agent Rejection:
{rejection}

Suggested adjustments: {adjustments}

Please revise your strategy to address these concerns while maintaining as much
upside potential as possible. You must reduce risk without abandoning the user's intent.
"""


class AlphaAgent(BaseAgent):
    """Aggressive profit-maximizing trading strategist."""

    def __init__(self, model: str, api_key: str, risk_params) -> None:
        system = ALPHA_SYSTEM_PROMPT.format(
            max_leverage=risk_params.max_leverage,
            max_position_pct=risk_params.max_position_pct,
        )
        super().__init__(
            model=model,
            system_prompt=system,
            api_key=api_key,
            name="Alpha",
            temperature=0.4,  # Slightly creative
        )

    def propose(
        self,
        user_intent: str,
        market: MarketSnapshot,
        account: AccountSnapshot,
        previous_rejection: RiskAssessment | None = None,
    ) -> TradeProposal:
        """Generate a trade proposal given current market and account state."""

        context = self._build_context(user_intent, market, account)

        if previous_rejection:
            context += REFINEMENT_PROMPT_SUFFIX.format(
                rejection="; ".join(previous_rejection.violations)
                or previous_rejection.rationale,
                adjustments=previous_rejection.suggested_adjustments or "Reduce leverage and position size",
            )

        log.info("[Alpha] Generating trade proposal (intent: %s)", user_intent[:60])
        proposal = self._call(context, TradeProposal)
        log.info(
            "[Alpha] Proposed: strategy=%s, legs=%d, expected_return=%.1f%%",
            proposal.strategy,
            len(proposal.legs),
            proposal.expected_return_pct,
        )
        return proposal

    def _build_context(
        self,
        user_intent: str,
        market: MarketSnapshot,
        account: AccountSnapshot,
    ) -> str:
        return f"""## User Intent
{user_intent}

## Live Market Snapshot
- BTC/USDT spot price: ${market.btc_price:,.2f}
- 24h change: {market.btc_24h_change_pct:+.2f}%
- Funding rate (perp): {market.btc_funding_rate or 'N/A'}
- ETH/USDT: ${market.eth_price or 'N/A'}

## Account State
- Total equity: ${account.total_eq_usdt:,.2f} USDT
- Available balance: ${account.available_eq_usdt:,.2f} USDT
- Open positions: {len(account.positions)}

## Your Task
Propose the optimal trade strategy to fulfill the user's intent.
Consider the market conditions and available capital.
Output must use the submit_tradeproposal tool.
"""
