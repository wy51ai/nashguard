# NashGuard — Architecture Deep Dive

## Overview

NashGuard is a trading safety protocol that wraps the OKX Agent Trade Kit in a
**Mixture-of-Agents (MoA) debate network** and a **Deterministic Risk Bounding (DRB)
sandbox**, ensuring zero-hallucination, anti-liquidation AI trading.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        User Input (plain language)                   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────┐
│                    NashGuard Orchestrator                             │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Phase 0: Snapshot                                           │    │
│  │  ┌──────────────────┐    ┌──────────────────────────────┐   │    │
│  │  │ market_get_ticker│    │   account_get_balance         │   │    │
│  │  │ market_get_fr    │ ←  │   swap_get_positions          │   │    │
│  │  └──────────────────┘    └──────────────────────────────┘   │    │
│  │            OKX Agent Trade Kit (MCP stdio)                    │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Phase 1: MoA Debate (up to 3 rounds)                        │    │
│  │                                                               │    │
│  │   ┌──────────────────┐     ┌──────────────────────────┐     │    │
│  │   │  α  Alpha Agent  │ ──► │  ⚠  Risk Agent           │     │    │
│  │   │  claude-haiku    │     │  claude-sonnet            │     │    │
│  │   │  "Max profit"    │ ◄── │  "Block dangerous"        │     │    │
│  │   └──────────────────┘     └──────────────────────────┘     │    │
│  │           ↓ (approved proposal)                               │    │
│  │   ┌──────────────────────────────────────────────────┐       │    │
│  │   │  ⚖  Judge Agent  (claude-sonnet)                 │       │    │
│  │   │  Consolidates debate → final TradeProposal + msg  │       │    │
│  │   └──────────────────────────────────────────────────┘       │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Phase 3: DRB Sandbox  (NON-LLM — pure mathematics)         │    │
│  │                                                               │    │
│  │   ┌────────────────┐   ┌─────────────────┐   ┌──────────┐  │    │
│  │   │ Black-Scholes  │   │ Monte Carlo GBM  │   │ Ed25519  │  │    │
│  │   │ Option Pricing │   │ 10,000 paths     │   │ Signing  │  │    │
│  │   └────────────────┘   └─────────────────┘   └──────────┘  │    │
│  │                                                               │    │
│  │   If max_drawdown > 20% OR leverage > 10x → HARD REJECT      │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Phase 4: Execution (signature-gated)                        │    │
│  │  Verifies Ed25519 sig → calls OKX Agent Trade Kit tools      │    │
│  │  spot_place_order / swap_place_order / option_place_order    │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. OKX MCP Client (`mcp_client/okx_mcp.py`)

NashGuard communicates with the OKX Agent Trade Kit via **subprocess stdio JSON-RPC 2.0**,
following the standard Model Context Protocol transport.

The client:
1. Spawns `okx-trade-mcp --modules market,spot,swap,option,account [--demo]`
2. Performs the MCP initialize handshake
3. Calls tools via `tools/call` JSON-RPC messages
4. Parses responses from `result.content[0].text`

In demo mode, the `x-simulated-trading: 1` header is automatically injected by the
MCP server, ensuring no real orders are placed during validation.

**Cross-validation design**: Market, Account, Spot, Swap, and Option data are all
fetched through the same MCP client, creating a self-consistent system state
(no stale data from different sources).

### 2. Alpha Agent (`agents/alpha.py`)

- **Model**: `claude-haiku-4-5-20251001` (fast, high throughput)
- **Role**: Profit-maximizing strategist
- **Output**: Structured `TradeProposal` via tool use (JSON schema enforced)
- **Self-correction**: On Risk rejection, Alpha receives the violation list and
  suggested adjustments, then proposes a revised strategy

### 3. Risk Agent (`agents/risk.py`)

- **Model**: `claude-sonnet-4-6` (careful, analytical)
- **Role**: Conservative gatekeeper — only approves strategies meeting all safety criteria
- **Hard limits** (any violation = `risk_approved=false`):
  - Leverage > 10x
  - Position size > 80% of available balance
  - Estimated drawdown > 20%
  - No stop-loss on leveraged positions
- **Output**: Structured `RiskAssessment` with `risk_approved` boolean, score/10, violation list

### 4. Judge Agent (`agents/judge.py`)

- **Model**: `claude-sonnet-4-6`
- **Role**: Final arbiter — consolidates the debate, writes user-facing explanation
- **Constraint**: Cannot override `risk_approved=false` — must propagate rejection
- **Output**: `JudgeDecision` with confidence score and plain-language `user_message`

### 5. DRB Sandbox (`sandbox/drb.py`)

The DRB sandbox is the **non-LLM mathematical firewall**. It operates entirely on
deterministic Python computations.

#### Black-Scholes (`sandbox/black_scholes.py`)

For option legs, NashGuard computes fair value and Greeks using the
standard Black-Scholes-Merton model:

```
d₁ = [ln(S/K) + (r + σ²/2)T] / (σ√T)
d₂ = d₁ - σ√T

Call = S·N(d₁) - K·e^(-rT)·N(d₂)
Put  = K·e^(-rT)·N(-d₂) - S·N(-d₁)
```

Implied volatility is solved via Newton-Raphson iteration when a market
price is available; otherwise the configurable `default_iv` is used.

#### Monte Carlo Simulation (`sandbox/monte_carlo.py`)

Portfolio P&L is simulated across 10,000 paths using Geometric Brownian Motion:

```
S(T) = S(0) · exp[(μ - σ²/2)T + σ√T·Z]   where Z ~ N(0,1)
```

Risk metrics computed:
- **Max Drawdown**: 1st percentile P&L loss (99% confidence worst case)
- **VaR 95%**: 5th percentile loss
- **CVaR / Expected Shortfall**: Mean of worst 5% scenarios
- **Liquidation Price**: Approximated as entry ± (1/leverage) × 90%

#### Ed25519 Signing

A local Ed25519 key pair is generated on first run (`~/.nashguard/signing_key.pem`,
permissions `0600`). The DRB result payload is signed with:

```python
signature = private_key.sign(json.dumps(payload, sort_keys=True).encode())
```

The orchestrator verifies this signature before calling any execution tools.
A tampered or unsigned result causes an immediate hard abort.

---

## MoA Debate Protocol

```
Round N:
  1. Alpha.propose(intent, market, account, [prev_rejection])
       → TradeProposal (strategy, legs, rationale, expected_return_pct)

  2. Risk.assess(proposal, market, account)
       → RiskAssessment (risk_approved, risk_score, violations, suggestions)

  3. if risk_approved: break → proceed to Judge
     else if round < max_rounds: go to Round N+1
     else: HARD REJECT

Judge.decide(proposal, assessment, rounds, intent)
  → JudgeDecision (approved, final_strategy, confidence, user_message)
```

The adversarial structure mirrors the academic MoA framework
(Wang et al., 2024: "Mixture-of-Agents Enhances LLM Capabilities").
Alpha maximizes expected return; Risk minimizes tail risk; Judge Pareto-optimizes.

---

## Security Properties

| Property | Mechanism |
|----------|-----------|
| No fat-finger orders | All sizes are percentages of available balance, computed from live `account_get_balance` |
| No leverage hallucination | Leverage is a bounded Pydantic field (`ge=1.0, le=125.0`); DRB hard-rejects > 10x |
| Cryptographic proof of risk check | Ed25519 signature on DRB payload; execution layer verifies before placing orders |
| Tamper detection | Signature covers `approved`, `max_drawdown_pct`, `var_95_pct`, `liquidation_price` |
| Demo-first validation | `--demo` flag sends `x-simulated-trading: 1` header; live keys only unlocked after DRB approval |
| Full audit trail | Every agent output, DRB metric, and execution result logged in `NashGuardResult.audit_log` |

---

## Replicability

The NashGuard protocol is designed as a **reusable safety wrapper**:

1. **Drop-in**: Any OKX Agent Trade Kit user can add NashGuard by setting
   `ANTHROPIC_API_KEY` and running `nashguard`.

2. **Configurable risk limits**: Edit `RiskParams` or set environment variables
   to adjust max drawdown, leverage, and position size limits.

3. **Model-agnostic**: The three agent roles can be powered by any Claude model.
   The DRB sandbox requires no LLM — it's pure Python math.

4. **Extensible**: Add new agent roles (e.g., a Liquidity agent) by subclassing
   `BaseAgent` and injecting into `NashGuardOrchestrator`.
