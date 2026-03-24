"""
NashGuard MCP Server

Exposes NashGuard as an MCP server with three tools that any agent can call:

  nashguard_snapshot   → fetch live OKX market + account data
  nashguard_validate   → run DRB sandbox on a trade proposal (pure math, no LLM)
  nashguard_execute    → verify DRB signature and execute via OKX Agent Trade Kit

The calling agent (Claude, GPT, Gemini, your own agent — anything) handles all
intelligence (the Alpha/Risk/Judge debate). NashGuard provides only the
deterministic safety infrastructure.

Start the server:
    nashguard-mcp           # reads config from .env / ~/.nashguard/
    nashguard-mcp --demo    # force demo mode

Register with Claude Desktop / Claude Code / Cursor:
    Add to your MCP client config:
      { "command": "nashguard-mcp", "args": ["--demo"] }
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .config import Config, RiskParams
from .mcp_client.okx_mcp import OKXMCPClient
from .models import AccountSnapshot, DRBResult, MarketSnapshot, TradeProposal
from .sandbox.drb import DRBSandbox

log = logging.getLogger(__name__)

# ── Server instance ────────────────────────────────────────────────────────────
mcp = FastMCP(
    "nashguard",
    instructions=(
        "NashGuard is a deterministic trading safety layer for OKX. "
        "WORKFLOW: (1) Call nashguard_snapshot to get live market+account data. "
        "(2) Use your own reasoning to form a trade proposal (debate internally as "
        "Alpha/Risk/Judge if desired). (3) Call nashguard_validate with your proposal "
        "— NashGuard will run a mathematical Monte Carlo + Black-Scholes risk sandbox "
        "and return a signed DRBResult. (4) If approved, call nashguard_execute with "
        "the original proposal and the signed DRBResult. The Ed25519 signature ensures "
        "only sandbox-approved trades can be executed. "
        "HARD LIMITS (configurable): max drawdown 20%, max leverage 10x, "
        "max single position 80% of balance."
    ),
)

# Module-level config and sandbox — initialized on first use
_config: Optional[Config] = None
_drb: Optional[DRBSandbox] = None


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def _get_drb() -> DRBSandbox:
    global _drb
    if _drb is None:
        cfg = _get_config()
        _drb = DRBSandbox(cfg.risk_params, cfg.key_store_path)
    return _drb


# ── Tool 1: nashguard_snapshot ─────────────────────────────────────────────────

@mcp.tool()
async def nashguard_snapshot() -> str:
    """
    Fetch live market and account data from OKX.

    Returns a JSON object with:
    - market: BTC/ETH prices, 24h change, funding rate
    - account: total equity, available balance, open positions
    - risk_params: current NashGuard hard limits (max_drawdown_pct, max_leverage, etc.)
    - demo_mode: whether NashGuard is in simulated trading mode

    Call this first before forming a trade proposal — it gives you the ground truth
    about current prices and available capital.
    """
    config = _get_config()
    async with OKXMCPClient(config) as client:
        market, account = await asyncio.gather(
            client.get_market_snapshot(),
            client.get_account_snapshot(),
        )

    return json.dumps(
        {
            "market": market.model_dump(),
            "account": account.model_dump(),
            "risk_params": {
                "max_drawdown_pct": config.risk_params.max_drawdown_pct,
                "max_leverage": config.risk_params.max_leverage,
                "max_position_pct": config.risk_params.max_position_pct,
            },
            "demo_mode": config.demo_mode,
        },
        indent=2,
    )


# ── Tool 2: nashguard_validate ─────────────────────────────────────────────────

@mcp.tool()
async def nashguard_validate(
    proposal: Dict[str, Any],
    market_snapshot: Optional[Dict[str, Any]] = None,
    account_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Run the DRB (Deterministic Risk Bounding) sandbox on a trade proposal.

    This is a pure mathematical evaluation — no LLM involved. It runs:
    - Monte Carlo simulation (10,000 GBM paths) for max drawdown / VaR / CVaR
    - Black-Scholes pricing for any option legs
    - Hard limit checks against configured risk parameters

    Arguments:
        proposal: Trade proposal object with fields:
            strategy: one of spot_buy | spot_sell | futures_long | futures_short |
                      covered_call | protective_put | call_buy | put_buy | combined | hold
            legs: list of trade legs, each with:
                module: "spot" | "swap" | "option"
                instId: e.g. "BTC-USDT" or "BTC-USD-251226-90000-C"
                side: "buy" | "sell"
                size_pct: 0-100 (% of available balance to allocate)
                leverage: 1.0-125.0 (default 1.0 for spot)
                ordType: "market" | "limit" (default "market")
                price: null for market orders
                tp_pct: optional take-profit % (e.g. 10.0 = 10% above entry)
                sl_pct: optional stop-loss % (e.g. 5.0 = 5% below entry)
            rationale: string explaining the strategy
            expected_return_pct: estimated upside %
            risk_summary: brief risk description
            tool_calls: list of {tool, arguments} for OKX execution (filled by you)

        market_snapshot: optional — provide the snapshot from nashguard_snapshot to
                         avoid a second OKX API call. If omitted, a fresh snapshot
                         is fetched automatically.

        account_snapshot: optional — same as market_snapshot.

    Returns a JSON DRBResult with:
        approved: bool — whether the trade passes all hard limits
        max_drawdown_pct: worst-case loss % (Monte Carlo 99th percentile)
        var_95_pct: Value-at-Risk at 95% confidence
        expected_shortfall_pct: CVaR (average loss in worst 5% scenarios)
        liquidation_price: estimated liquidation price for leveraged positions
        rejection_reason: human-readable reason if approved=false
        signature_hex: Ed25519 signature — pass this to nashguard_execute
        details: option Greeks, simulation stats, public key

    IMPORTANT: If approved=false, do NOT call nashguard_execute. Revise the
    proposal (reduce size_pct, reduce leverage, or change strategy) and re-validate.
    """
    config = _get_config()
    drb = _get_drb()

    # Parse proposal
    trade_proposal = TradeProposal.model_validate(proposal)

    # Get snapshots (reuse provided ones or fetch fresh)
    if market_snapshot and account_snapshot:
        market = MarketSnapshot.model_validate(market_snapshot)
        account = AccountSnapshot.model_validate(account_snapshot)
    else:
        async with OKXMCPClient(config) as client:
            if market_snapshot:
                market = MarketSnapshot.model_validate(market_snapshot)
                account = await client.get_account_snapshot()
            elif account_snapshot:
                market = await client.get_market_snapshot()
                account = AccountSnapshot.model_validate(account_snapshot)
            else:
                market, account = await asyncio.gather(
                    client.get_market_snapshot(),
                    client.get_account_snapshot(),
                )

    # Run DRB sandbox (pure math, no LLM)
    drb_result: DRBResult = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: drb.evaluate(trade_proposal, market, account),
    )

    return json.dumps(drb_result.model_dump(), indent=2)


# ── Tool 3: nashguard_execute ──────────────────────────────────────────────────

@mcp.tool()
async def nashguard_execute(
    proposal: Dict[str, Any],
    drb_result: Dict[str, Any],
) -> str:
    """
    Execute a trade that has been validated by nashguard_validate.

    This tool verifies the Ed25519 signature from nashguard_validate to
    cryptographically confirm that:
    1. The DRB sandbox genuinely ran and approved this trade
    2. The risk result has not been tampered with

    If the signature is invalid or approved=false in the DRB result, execution
    is hard-blocked — no OKX API calls are made.

    Arguments:
        proposal: The same proposal dict you passed to nashguard_validate.
                  Must contain tool_calls — the list of OKX tools to execute.
                  Example tool_call:
                    { "tool": "spot_place_order",
                      "arguments": { "instId": "BTC-USDT", "side": "buy",
                                     "ordType": "market", "sz": "0.01" } }
        drb_result: The full DRBResult JSON returned by nashguard_validate.
                    Must include signature_hex.

    Returns a JSON object with:
        executed: bool
        rejection_reason: string if not executed
        mode: "DEMO" or "LIVE"
        results: list of {tool, success, result/error} for each executed tool call
        audit: list of log lines
    """
    config = _get_config()
    drb = _get_drb()

    trade_proposal = TradeProposal.model_validate(proposal)
    drb_validated = DRBResult.model_validate(drb_result)

    # ── Cryptographic gate: verify Ed25519 signature ───────────────────────────
    if not drb.verify(drb_validated):
        return json.dumps(
            {
                "executed": False,
                "rejection_reason": (
                    "DRB signature verification FAILED. "
                    "The risk result may have been tampered with. "
                    "Re-run nashguard_validate to get a fresh signed result."
                ),
            }
        )

    # ── Hard limit gate: check approved flag in the signed result ──────────────
    if not drb_validated.approved:
        return json.dumps(
            {
                "executed": False,
                "rejection_reason": (
                    f"DRB sandbox rejected this trade: {drb_validated.rejection_reason}. "
                    f"Max drawdown {drb_validated.max_drawdown_pct:.1f}% exceeded limit. "
                    "Revise your proposal and re-validate."
                ),
            }
        )

    if not trade_proposal.tool_calls:
        return json.dumps(
            {
                "executed": False,
                "rejection_reason": (
                    "proposal.tool_calls is empty. "
                    "Populate it with the OKX tool calls to execute "
                    "(e.g. spot_place_order, swap_place_order)."
                ),
            }
        )

    # ── Execute via OKX Agent Trade Kit ───────────────────────────────────────
    mode = "DEMO" if config.demo_mode else "LIVE"
    results = []
    audit = [
        f"[GATE] DRB signature verified ✓",
        f"[GATE] approved={drb_validated.approved}, "
        f"max_dd={drb_validated.max_drawdown_pct:.1f}%, "
        f"VaR95={drb_validated.var_95_pct:.1f}%",
        f"[EXEC] Mode={mode}, legs={len(trade_proposal.tool_calls)}",
    ]

    async with OKXMCPClient(config) as client:
        for tc in trade_proposal.tool_calls:
            audit.append(f"[EXEC] → {tc.tool}({json.dumps(tc.arguments)})")
            try:
                result = await client.call_tool(tc.tool, tc.arguments)
                results.append({"tool": tc.tool, "success": True, "result": result})
                audit.append(f"[EXEC]   ✓ OK")
            except Exception as e:
                results.append({"tool": tc.tool, "success": False, "error": str(e)})
                audit.append(f"[EXEC]   ✗ ERROR: {e}")

    all_ok = all(r["success"] for r in results)
    return json.dumps(
        {
            "executed": all_ok,
            "mode": mode,
            "results": results,
            "audit": audit,
            "drb_summary": {
                "max_drawdown_pct": drb_validated.max_drawdown_pct,
                "var_95_pct": drb_validated.var_95_pct,
                "signature_hex": drb_validated.signature_hex[:32] + "...",
            },
        },
        indent=2,
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def run_server(demo: bool = False, live: bool = False) -> None:
    """Start the NashGuard MCP server (stdio transport)."""
    import argparse, sys

    parser = argparse.ArgumentParser(
        description="nashguard-mcp — NashGuard MCP Server\n\n"
        "Exposes nashguard_snapshot, nashguard_validate, nashguard_execute\n"
        "as MCP tools callable by any agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--demo", action="store_true", help="Force demo/simulated trading mode")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    config = _get_config()
    if args.demo:
        config.demo_mode = True
    if args.live:
        config.demo_mode = False

    mcp.run(transport="stdio")
