"""
Judge Agent — The Final Arbiter

Judge's role: Receive the full Alpha-Risk debate transcript and render
a final, user-facing decision. The Judge:
  1. Validates that Risk's approval is genuine (not bypassed)
  2. Consolidates the final strategy (may adjust Alpha's proposal)
  3. Writes the user-facing explanation — clear, honest, actionable
  4. Sets confidence score

The Judge cannot override Risk's hard rejections. If risk_approved=False
after max_debate_rounds, Judge's approved must be False.

Model: claude-sonnet-4-6 (balanced and articulate)
"""

from __future__ import annotations

import logging

from ..models import JudgeDecision, RiskAssessment, TradeProposal
from .base import BaseAgent

log = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = """You are Judge, the final arbiter at NashGuard.

You receive a trade proposal from Alpha and a risk assessment from Risk.
Your job is to:
1. Render the final verdict on whether to proceed
2. Produce a clear, honest explanation for the user
3. If approved, finalize the exact strategy to execute

## Rules you MUST follow:
- If risk_approved is False: your approved MUST be False
- If risk_approved is True: you may approve with confidence 0.6-1.0
- The user_message must explain WHAT was decided and WHY in plain language
- If rejecting a dangerous request, explain what safer alternative was considered
- If executing a modified strategy, explain exactly how it differs from user's request

## Writing the user_message:
- Be direct and honest. Don't be preachy.
- Quantify the risk in dollar terms (not just percentages)
- If rejected: suggest the closest safe alternative
- If approved: give key risk numbers (max loss in USD, liquidation price)
- Maximum 3 sentences. No bullet points.

Use the submit_judgedecision tool to submit your decision.
"""


class JudgeAgent(BaseAgent):
    """Final arbiter that consolidates the MoA debate into a binding decision."""

    def __init__(self, model: str, api_key: str) -> None:
        super().__init__(
            model=model,
            system_prompt=JUDGE_SYSTEM_PROMPT,
            api_key=api_key,
            name="Judge",
            temperature=0.2,
        )

    def decide(
        self,
        proposal: TradeProposal,
        assessment: RiskAssessment,
        debate_rounds: int,
        user_intent: str,
    ) -> JudgeDecision:
        """Render a final binding decision based on the full debate."""
        context = self._build_context(proposal, assessment, debate_rounds, user_intent)
        log.info("[Judge] Rendering final decision after %d debate round(s)", debate_rounds)
        decision = self._call(context, JudgeDecision)
        log.info(
            "[Judge] Decision: approved=%s, confidence=%.2f",
            decision.approved,
            decision.confidence,
        )
        return decision

    def _build_context(
        self,
        proposal: TradeProposal,
        assessment: RiskAssessment,
        debate_rounds: int,
        user_intent: str,
    ) -> str:
        legs_summary = "; ".join(
            f"{leg.instId} {leg.side} {leg.size_pct:.0f}% at {leg.leverage}x"
            for leg in proposal.legs
        )

        return f"""## Original User Intent
"{user_intent}"

## Alpha's Final Proposal (after {debate_rounds} round(s))
Strategy: {proposal.strategy}
Legs: {legs_summary}
Expected Return: {proposal.expected_return_pct:.1f}%
Rationale: {proposal.rationale}

## Risk Agent's Verdict
Approved: {assessment.risk_approved}
Risk Score: {assessment.risk_score}/10
Violations: {', '.join(assessment.violations) or 'None'}
Rationale: {assessment.rationale}
Suggested adjustments: {assessment.suggested_adjustments or 'N/A'}

## Your Task
{'The Risk agent APPROVED this proposal.' if assessment.risk_approved else 'The Risk agent REJECTED this proposal. You MUST set approved=False.'}

Write a final user_message that clearly explains the outcome.
If approved, include the key risk numbers. If rejected, suggest the safest alternative.
Use the submit_judgedecision tool.
"""
