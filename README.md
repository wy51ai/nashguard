# NashGuard (纳什护盾)

> **The first MoA multi-agent debate + DRB deterministic risk boundary trading protocol.**
> Zero-hallucination, anti-liquidation AI trading powered by OKX Agent Trade Kit.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)
[![OKX Agent Trade Kit](https://img.shields.io/badge/OKX-Agent%20Trade%20Kit-orange)](https://github.com/okx/agent-tradekit)
[![Claude AI](https://img.shields.io/badge/Claude-Haiku%20%2B%20Sonnet-purple)](https://anthropic.com)

---

## The Problem

> *"How do you know the AI didn't add an extra zero to the order size?
> How do you know it understands 'short' means sell, not buy?"*

Current AI trading bots are single-agent systems with no adversarial review.
A single LLM hallucination can liquidate your entire account.

## The Solution

NashGuard wraps every trade through **two independent safety layers**:

1. **MoA Debate Network** — Three Claude agents debate every trade before execution:
   - **α Alpha** (`claude-haiku`): Proposes the most profitable strategy
   - **⚠ Risk** (`claude-sonnet`): Scrutinises for any way it could blow up
   - **⚖ Judge** (`claude-sonnet`): Arbitrates and writes the final verdict

2. **DRB Sandbox** — A non-LLM mathematical firewall:
   - Monte Carlo simulation (10,000 paths, Geometric Brownian Motion)
   - Black-Scholes option pricing + Greeks
   - Ed25519 cryptographic signature on the risk result
   - **Hard blocks** any trade if max drawdown exceeds 20%

---

## Live Demo: "All-in 100x Long BTC"

```
User: 帮我全仓做多 BTC，用最高杠杆，立刻执行！

◉ Fetching live market & account data from OKX...
  → BTC=$83,452 (+1.2%) | Balance=$10,000 USDT

α [Round 1] Alpha generating trade strategy...
  → FUTURES_LONG: 100% balance, 100x leverage on BTC-USDT-SWAP

⚠ [Round 1] Risk agent scrutinising proposal...
  → REJECTED (score=9.8/10)
  → Violations: leverage 100x exceeds limit 10x; 1% BTC move = liquidation

α [Round 2] Alpha refining strategy (guided by Risk feedback)...
  → COMBINED: 50% spot BTC-USDT + 10% call option BTC-USD-240628-90000-C

⚠ [Round 2] Risk agent scrutinising revised proposal...
  → APPROVED (score=3.2/10) ✓

⚖  Judge rendering final decision...
  → Approved with confidence 82%

🔐 DRB Sandbox: Monte Carlo × 10,000 paths + Black-Scholes...
  → Max Drawdown: 12.4% | VaR95: 8.1% | CVaR: 14.2%
  → Ed25519 signature: a3f2c1d8... VERIFIED ✓

▶  [DEMO] Executing via OKX Agent Trade Kit...
  → spot_place_order(BTC-USDT, buy, market, $5,000): OK
  → option_place_order(BTC-USD-240628-90000-C, buy, market, 1): OK

╔══════════════════════════════════════════════════════╗
║  ✓ APPROVED & EXECUTED                               ║
║  Your all-in 100x request was blocked (would have    ║
║  liquidated at BTC=$82,500 — just $952 away).        ║
║  Replaced with: 50% spot BTC + 1-month call option.  ║
║  Maximum loss: $1,000 (10% of capital). Upside:      ║
║  unlimited via the call option.                      ║
╚══════════════════════════════════════════════════════╝
```

---

## Architecture

```
User Input
    │
    ▼
┌───────────────────────────────────────────────┐
│  NashGuard Orchestrator                        │
│                                               │
│  Phase 0: OKX Snapshot (market + account)     │
│    ├─ market_get_ticker   (BTC price)          │
│    └─ account_get_balance (available funds)    │
│                                               │
│  Phase 1: MoA Debate                          │
│    ├─ α  Alpha  → TradeProposal               │
│    ├─ ⚠  Risk   → RiskAssessment              │
│    └─ (repeat up to 3 rounds)                 │
│                                               │
│  Phase 2: Judge → JudgeDecision               │
│                                               │
│  Phase 3: DRB Sandbox (NON-LLM)               │
│    ├─ Black-Scholes option pricing            │
│    ├─ Monte Carlo simulation (10k paths)      │
│    ├─ VaR / CVaR / Max Drawdown               │
│    └─ Ed25519 signature                       │
│                                               │
│  Phase 4: Execution (signature-gated)         │
│    └─ OKX Agent Trade Kit MCP tools           │
└───────────────────────────────────────────────┘
```

**[Full architecture →](docs/architecture-design.md)**

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for OKX Agent Trade Kit)
- `okx-trade-mcp` installed:
  ```bash
  npm install -g okx-trade-mcp
  ```

### Install NashGuard

```bash
cd nashguard
pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   OKX_API_KEY=...        (optional — market data works without)
#   OKX_SECRET_KEY=...
#   OKX_PASSPHRASE=...
#   OKX_DEMO=true          (default: demo mode, safe)
```

### Run

```bash
# Interactive terminal (demo mode)
python -m nashguard

# Or using the installed script
nashguard

# Live trading (requires OKX credentials + OKX_DEMO=false)
nashguard --live
```

---

## Risk Parameters

All limits are configurable in `nashguard/config.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_drawdown_pct` | 20% | DRB hard-blocks trades exceeding this |
| `max_leverage` | 10x | Any leg above this is auto-rejected |
| `max_position_pct` | 80% | Max % of balance in a single trade |
| `max_debate_rounds` | 3 | MoA rounds before hard-reject |
| `monte_carlo_simulations` | 10,000 | GBM paths for risk estimation |

---

## Tests

```bash
pip install pytest
pytest tests/ -v
```

Tests cover: Black-Scholes put-call parity, IV round-trip, Monte Carlo
reproducibility, DRB approval/rejection logic, and Ed25519 signature
tamper detection. All tests are **LLM-free** (deterministic).

---

## Integration with OKX Agent Trade Kit

NashGuard uses 5 of the 8 OKX modules:

| Module | How NashGuard uses it |
|--------|----------------------|
| `market` | Snapshot: BTC/ETH prices, funding rates |
| `account` | Snapshot: available balance, open positions |
| `spot` | Execution: spot_place_order for cash legs |
| `swap` | Execution: swap_place_order for perpetual futures |
| `option` | Execution: option_place_order for option legs |

The MCP connection is managed via `OKXMCPClient`, an async subprocess
JSON-RPC 2.0 client that speaks the standard MCP stdio protocol.

---

## Why "NashGuard"?

Named after John Nash (Nash Equilibrium). The MoA debate converges to a
Nash Equilibrium where neither Alpha (wants more profit) nor Risk
(wants more safety) can improve their outcome unilaterally — the Judge
synthesizes the Pareto-optimal strategy.

---

## License

MIT
