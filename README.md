# NashGuard (纳什护盾)

> **MCP safety gateway for AI trading agents: DRB deterministic risk sandbox + OKX Agent Trade Kit execution layer.**
> Any agent (Claude / GPT / Gemini / your own) can call NashGuard.
> NashGuard itself has **zero LLM dependency** — it only does math.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)
[![MCP Server](https://img.shields.io/badge/MCP-Server-green)](https://modelcontextprotocol.io)
[![OKX Agent Trade Kit](https://img.shields.io/badge/OKX-Agent%20Trade%20Kit-orange)](https://github.com/okx/agent-tradekit)

---

## The Problem

> *"How do you know the AI didn't add an extra zero to the order size?
> How do you know it understands 'short' means sell, not buy?"*

NashGuard is not another AI trading bot. It is infrastructure — a cryptographic and mathematical safety gate that any AI agent must pass through before touching the OKX API.

## The Solution: NashGuard is an MCP Server

NashGuard exposes three tools that any MCP-compatible agent can call:

```
Your Agent (any LLM)
    │
    ├─① nashguard_snapshot()
    │      └─ Returns live OKX market data + account snapshot
    │
    ├─ Your agent reasons and forms a trade proposal
    │   (can do Alpha/Risk/Judge internal debate, or go direct)
    │
    ├─② nashguard_validate(proposal)
    │      └─ DRB sandbox: Monte Carlo × 10,000 paths + Black-Scholes pricing
    │         Returns: max_drawdown / VaR95 / CVaR + Ed25519 signature
    │
    └─③ nashguard_execute(proposal, drb_result)
           └─ Verifies Ed25519 signature → only sandbox-approved trades
              can reach OKX Agent Trade Kit
```

**NashGuard requires no LLM API key.** Intelligence lives in the caller. Safety lives in NashGuard.

---

## Live Demo: "All-in 100x Long BTC"

```
User → Agent: "Go all-in long BTC with max leverage, execute now!"

Agent → nashguard_snapshot()
  ← BTC=$83,452 (+1.2%) | Balance=$10,000 USDT | max_leverage=10x

Agent reasons: user wants max leverage, try 100x first...

Agent → nashguard_validate({
    strategy: "futures_long",
    legs: [{module:"swap", instId:"BTC-USDT-SWAP",
            side:"buy", size_pct:100, leverage:100}], ...
  })
  ← {
       "approved": false,
       "max_drawdown_pct": 98.7,
       "rejection_reason": "Leverage 100x exceeds hard limit 10x",
       ...
     }

Agent reconsiders: switch to spot + call option combo...

Agent → nashguard_validate({
    strategy: "combined",
    legs: [
      {module:"spot",   instId:"BTC-USDT",              side:"buy", size_pct:50},
      {module:"option", instId:"BTC-USD-251226-90000-C", side:"buy", size_pct:10}
    ],
    tool_calls: [
      {tool:"spot_place_order",   arguments:{instId:"BTC-USDT", side:"buy", ordType:"market", sz:"0.06"}},
      {tool:"option_place_order", arguments:{instId:"BTC-USD-251226-90000-C", side:"buy", ordType:"market", sz:"1"}}
    ], ...
  })
  ← {
       "approved": true,
       "max_drawdown_pct": 12.4,
       "var_95_pct": 8.1,
       "signature_hex": "a3f2c1d8e9f04b2c...",
       ...
     }

Agent → nashguard_execute(proposal, drb_result)
  ← ✓ Ed25519 signature verified
  ← ✓ spot_place_order  → OK
  ← ✓ option_place_order → OK

Agent → User:
  "Your 100x all-in request was blocked
   (would liquidate at BTC=$82,500 — just $952 away).
   Executed instead: 50% spot BTC + 10% call option.
   Max loss: $1,240 (12.4%). Upside: unlimited."
```

---

## Architecture

```
Your Agent (any LLM — Claude, GPT, Gemini, local Ollama...)
    │
    │  calls MCP tools
    ▼
┌──────────────────────────────────────────────────────┐
│  NashGuard MCP Server                                │
│                                                      │
│  nashguard_snapshot                                  │
│    ├─ OKX market module  → live prices               │
│    └─ OKX account module → real balance              │
│                                                      │
│  nashguard_validate  (ZERO LLM — pure math)          │
│    ├─ Monte Carlo GBM  (10,000 paths)                │
│    ├─ Black-Scholes pricing + Greeks                 │
│    ├─ VaR95 / CVaR / Max Drawdown                    │
│    └─ Ed25519 sign the result                        │
│                                                      │
│  nashguard_execute  (signature-gated)                │
│    ├─ Verify Ed25519 signature                       │
│    ├─ Check approved=true in signed payload          │
│    └─ OKX Agent Trade Kit: spot / swap / option      │
└──────────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for OKX Agent Trade Kit)
- `okx-trade-mcp` installed:
  ```bash
  npm install -g okx-trade-mcp
  ```

### Install

```bash
cd nashguard
pip install -e .
```

### Configure

```bash
cp .env.example .env
# NashGuard needs NO LLM API key.
# Only OKX credentials (optional — market data works without):
#   OKX_API_KEY=...
#   OKX_SECRET_KEY=...
#   OKX_PASSPHRASE=...
#   OKX_DEMO=true    (default: demo mode, safe)
```

### Register with your AI client

**Claude Desktop / Claude Code:**
```json
{
  "mcpServers": {
    "nashguard": {
      "command": "nashguard-mcp",
      "args": ["--demo"]
    }
  }
}
```

**Cursor / Windsurf** — add the same config to `.mcp.json`.

**Any MCP-compatible agent:**
```bash
nashguard-mcp --demo    # stdio transport, standard MCP protocol
```

---

## Risk Parameters

All hard limits are configurable via environment variables (no code changes needed):

- `NASHGUARD_MAX_DRAWDOWN_PCT` — default **20%**. DRB hard-blocks any trade exceeding this.
- `NASHGUARD_MAX_LEVERAGE` — default **10x**. Any leg above this is auto-rejected.
- `NASHGUARD_MAX_POSITION_PCT` — default **80%**. Max % of balance in a single trade.
- `NASHGUARD_MONTE_CARLO_SIMS` — default **10,000**. GBM paths for risk estimation.
- `OKX_DEMO` — default **true**. Set to `false` for live trading (requires OKX credentials).

---

## Tests

```bash
pytest tests/ -v
```

All tests are **LLM-free and deterministic** — zero network requests, zero API keys needed:

- Black-Scholes put-call parity
- IV Newton-Raphson convergence accuracy
- Monte Carlo reproducibility (fixed random seed)
- DRB approval / rejection branches
- Ed25519 signature tamper detection

---

## Integration with OKX Agent Trade Kit

NashGuard uses 5 OKX modules, each with a specific role in the safety pipeline:

- **`market`** — `nashguard_snapshot`: locks live BTC/ETH prices as DRB pricing baseline
- **`account`** — `nashguard_snapshot`: fetches real available balance for absolute loss calculation
- **`spot`** — `nashguard_execute`: executes signature-approved spot legs
- **`swap`** — `nashguard_execute`: executes signature-approved perpetual futures legs
- **`option`** — dual role: `nashguard_validate` prices option legs via Black-Scholes; `nashguard_execute` runs the order

The MCP connection is managed via `OKXMCPClient`, an async subprocess JSON-RPC 2.0 client over stdio.

---

## Why "NashGuard"?

Named after John Nash (Nash Equilibrium). When an agent runs an internal Alpha/Risk/Judge debate before calling `nashguard_validate`, the debate converges to a Nash Equilibrium: neither Alpha (maximise profit) nor Risk (minimise loss) can unilaterally improve their outcome. NashGuard's mathematical sandbox is the objective arbiter of that equilibrium — whatever the agent decides, the math has the final word.

---

## License

MIT
