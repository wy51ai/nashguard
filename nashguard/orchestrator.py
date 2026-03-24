"""
NashGuard Orchestrator

The central coordinator that runs the full MoA debate + DRB validation pipeline:

  Phase 0: Market & Account Snapshot (via OKX MCP)
  Phase 1: MoA Debate Loop
    ├─ Alpha proposes strategy
    ├─ Risk assesses and potentially rejects
    └─ (loop up to max_debate_rounds)
  Phase 2: Judge renders binding decision
  Phase 3: DRB Sandbox validation (mathematical, non-LLM)
  Phase 4: Execution via OKX Agent Trade Kit (demo or live)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from .agents.alpha import AlphaAgent
from .agents.judge import JudgeAgent
from .agents.risk import RiskAgent
from .config import Config
from .mcp_client.okx_mcp import OKXMCPClient
from .models import (
    AccountSnapshot,
    DRBResult,
    JudgeDecision,
    MarketSnapshot,
    NashGuardResult,
    RiskAssessment,
    TradeProposal,
)
from .sandbox.drb import DRBSandbox

log = logging.getLogger(__name__)


class NashGuardOrchestrator:
    """
    Coordinates the full NashGuard pipeline.

    Accepts a `progress_callback` for real-time UI updates:
        callback(stage: str, detail: str)
    """

    def __init__(
        self,
        config: Config,
        progress_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.config = config
        self._cb = progress_callback or (lambda stage, detail: None)

        # api_key is optional — litellm reads provider keys from env vars automatically.
        # Passing it explicitly here covers the case where the user set it in Config
        # directly rather than via environment variables.
        api_key = config.anthropic_api_key or config.openai_api_key or config.gemini_api_key
        self._alpha = AlphaAgent(config.alpha_model, api_key, config.risk_params)
        self._risk = RiskAgent(config.risk_model, api_key, config.risk_params)
        self._judge = JudgeAgent(config.judge_model, api_key)
        self._drb = DRBSandbox(config.risk_params, config.key_store_path)

    async def run(self, user_intent: str) -> NashGuardResult:
        """
        Execute the full NashGuard pipeline for a user trading intent.
        Returns a NashGuardResult with full audit trail.
        """
        audit: list[str] = []
        result = NashGuardResult(
            user_intent=user_intent,
            demo_mode=self.config.demo_mode,
            audit_log=audit,
        )

        async with OKXMCPClient(self.config) as mcp:
            # ─── Phase 0: Snapshot ───────────────────────────────────────────
            self._cb("snapshot", "Fetching live market & account data from OKX...")
            market, account = await asyncio.gather(
                mcp.get_market_snapshot(),
                mcp.get_account_snapshot(),
            )

            audit.append(
                f"[SNAPSHOT] BTC=${market.btc_price:,.0f} ({market.btc_24h_change_pct:+.2f}%) "
                f"| Balance=${account.available_eq_usdt:,.2f} USDT "
                f"| Credentials={'YES' if account.has_credentials else 'NO (mock)'}"
            )

            # ─── Phase 1: MoA Debate ─────────────────────────────────────────
            proposal: Optional[TradeProposal] = None
            assessment: Optional[RiskAssessment] = None
            debate_rounds = 0
            last_rejection: Optional[RiskAssessment] = None

            for round_num in range(1, self.config.risk_params.max_debate_rounds + 1):
                debate_rounds = round_num

                # Alpha proposes
                self._cb(
                    "alpha",
                    f"[Round {round_num}] Alpha is generating trade strategy...",
                )
                proposal = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda r=last_rejection: self._alpha.propose(
                        user_intent, market, account, previous_rejection=r
                    ),
                )
                audit.append(
                    f"[Alpha R{round_num}] Strategy={proposal.strategy}, "
                    f"legs={len(proposal.legs)}, expected_return={proposal.expected_return_pct:.1f}%"
                )

                # Risk assesses
                self._cb(
                    "risk",
                    f"[Round {round_num}] Risk agent scrutinising proposal (score/10)...",
                )
                assessment = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda p=proposal: self._risk.assess(p, market, account),
                )
                audit.append(
                    f"[Risk R{round_num}] approved={assessment.risk_approved}, "
                    f"score={assessment.risk_score:.1f}, "
                    f"violations={assessment.violations or 'none'}"
                )

                if assessment.risk_approved:
                    audit.append(f"[Risk] Approved after {round_num} round(s)")
                    break

                last_rejection = assessment
                audit.append(
                    f"[Risk] Rejected — violations: {'; '.join(assessment.violations)}"
                )

                if round_num == self.config.risk_params.max_debate_rounds:
                    # Hard reject — all rounds exhausted
                    audit.append("[SYSTEM] Max debate rounds reached — HARD REJECT")
                    result.debate_rounds = debate_rounds
                    result.alpha_proposal = proposal
                    result.risk_assessment = assessment
                    result.rejected = True
                    result.rejection_reason = (
                        f"Strategy rejected after {round_num} debate rounds. "
                        f"Final violations: {'; '.join(assessment.violations)}"
                    )
                    return result

            result.debate_rounds = debate_rounds
            result.alpha_proposal = proposal
            result.risk_assessment = assessment

            # ─── Phase 2: Judge ──────────────────────────────────────────────
            self._cb("judge", "Judge agent rendering final binding decision...")
            decision: JudgeDecision = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._judge.decide(
                    proposal, assessment, debate_rounds, user_intent  # type: ignore[arg-type]
                ),
            )
            result.judge_decision = decision
            audit.append(
                f"[Judge] approved={decision.approved}, confidence={decision.confidence:.2f}"
            )

            if not decision.approved:
                result.rejected = True
                result.rejection_reason = decision.rationale
                return result

            # ─── Phase 3: DRB Sandbox ────────────────────────────────────────
            self._cb(
                "drb",
                "DRB Sandbox running Monte Carlo simulation + Black-Scholes pricing...",
            )
            drb_result: DRBResult = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._drb.evaluate(
                    decision.final_strategy or proposal,  # type: ignore[arg-type]
                    market,
                    account,
                ),
            )
            result.drb_result = drb_result
            audit.append(
                f"[DRB] max_dd={drb_result.max_drawdown_pct:.1f}%, "
                f"VaR95={drb_result.var_95_pct:.1f}%, "
                f"approved={drb_result.approved}, "
                f"sig={drb_result.signature_hex[:16]}..."
            )

            if not drb_result.approved:
                result.rejected = True
                result.rejection_reason = (
                    f"DRB sandbox blocked execution: {drb_result.rejection_reason}"
                )
                return result

            # Verify signature before execution
            if not self._drb.verify(drb_result):
                audit.append("[DRB] SIGNATURE VERIFICATION FAILED — aborting")
                result.rejected = True
                result.rejection_reason = "DRB signature verification failed (tamper detected)"
                return result

            audit.append("[DRB] Ed25519 signature verified ✓")

            # ─── Phase 4: Execution ───────────────────────────────────────────
            final_plan = decision.final_strategy or proposal
            mode_label = "DEMO" if self.config.demo_mode else "LIVE"
            self._cb(
                "execute",
                f"[{mode_label}] Executing strategy via OKX Agent Trade Kit...",
            )

            execution_results = []
            for tool_call in final_plan.tool_calls:
                self._cb("execute", f"→ {tool_call.tool}({tool_call.arguments})")
                try:
                    exec_result = await mcp.call_tool(
                        tool_call.tool, tool_call.arguments
                    )
                    execution_results.append(
                        {"tool": tool_call.tool, "result": exec_result, "success": True}
                    )
                    audit.append(f"[EXEC] {tool_call.tool}: OK")
                except Exception as e:
                    audit.append(f"[EXEC] {tool_call.tool}: ERROR — {e}")
                    execution_results.append(
                        {"tool": tool_call.tool, "error": str(e), "success": False}
                    )

            result.execution_results = execution_results

        return result
