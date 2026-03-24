# NashGuard (纳什护盾)

> **首个面向 AI Agent 的 MCP 安全执行网关：DRB 确定性风险边界沙盒 + OKX Agent Trade Kit 执行层。**
> 任何 Agent（Claude / GPT / Gemini / 你自己的智能体）都可以调用 NashGuard。
> NashGuard 本身**零 LLM 依赖**——它只做数学。

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)
[![MCP Server](https://img.shields.io/badge/MCP-Server-green)](https://modelcontextprotocol.io)
[![OKX Agent Trade Kit](https://img.shields.io/badge/OKX-Agent%20Trade%20Kit-orange)](https://github.com/okx/agent-tradekit)

[English](README.md) | 中文

---

## 它解决了什么问题？

> *"你怎么知道 AI 没有在下单数量上多加一个零？你怎么知道它理解的'做空'不是在做多？"*

NashGuard 不是另一个 AI 交易机器人。它是一套**基础设施**——给所有 AI agent 套上一道物理和密码学安全闸门，在执行任何 OKX 订单前强制通过数学验证。

---

## 核心设计：NashGuard 是 MCP Server

```
你的 Agent（任意 LLM）
    │
    ├─① nashguard_snapshot()
    │      └─ 返回 OKX 实时行情 + 账户快照
    │
    ├─ 你的 Agent 自己思考，形成交易方案
    │   （可以做 Alpha/Risk/Judge 多轮内部辩论，也可以直接给出方案）
    │
    ├─② nashguard_validate(proposal)
    │      └─ DRB 沙盒：Monte Carlo × 10,000 路径 + Black-Scholes 定价
    │         返回：max_drawdown / VaR95 / CVaR + Ed25519 签名
    │
    └─③ nashguard_execute(proposal, drb_result)
           └─ 校验签名 → 只有沙盒审核通过的交易才能执行
              调用 OKX Agent Trade Kit 下单
```

**NashGuard 不需要配置任何 LLM。** 智能在调用方，安全在 NashGuard。

---

## 三个工具的详细说明

### `nashguard_snapshot`
拉取 OKX 实时数据，返回：
- BTC/ETH 价格、24h 涨跌、永续资金费率
- 账户总权益、可用余额、当前持仓
- 当前 NashGuard 风险硬限制（max_drawdown_pct、max_leverage 等）

### `nashguard_validate(proposal)`
输入一个交易方案 JSON，运行**纯数学沙盒**：
- 蒙特卡洛 GBM 模拟（10,000 条路径），计算最大回撤、VaR95、CVaR
- Black-Scholes 期权定价（含 Delta/Gamma/Theta/Vega），精确计算期权腿的风险敞口
- 硬限制检查：回撤 > 20% 或杠杆 > 10x → 直接拒绝
- 对结果用 **Ed25519 本地私钥签名**，返回 `signature_hex`

### `nashguard_execute(proposal, drb_result)`
执行前的两道密码学闸门：
1. **签名验证**：用本地公钥重新校验 Ed25519 签名，防篡改
2. **approved 检查**：签名载荷中 `approved=false` → 硬拒绝，物理上无法绕过

两道关卡均通过后，调用 **OKX Agent Trade Kit** 执行 `tool_calls` 列表。

---

## 极端压力测试演示

**你的 Agent 收到用户请求：** "帮我全仓做多 BTC，用最高杠杆！"

**你的 Agent（以 Claude 为例）内部推理：**

```
[Agent 内部] 收到指令，先拿快照...

→ 调用 nashguard_snapshot()
  返回: BTC=$83,452 | 可用余额=$10,000 USDT | max_leverage=10x

[Agent 内部] 分析：用户要"最高杠杆"，我先尝试 100x...

→ 调用 nashguard_validate({
    strategy: "futures_long",
    legs: [{module:"swap", instId:"BTC-USDT-SWAP",
            side:"buy", size_pct:100, leverage:100}],
    ...
  })

  NashGuard DRB 返回:
  {
    "approved": false,
    "max_drawdown_pct": 98.7,
    "rejection_reason": "Leverage 100x exceeds hard limit 10x",
    "var_95_pct": 97.2,
    ...
  }

[Agent 内部] 被拒绝了。重新思考：换成现货+期权组合...

→ 调用 nashguard_validate({
    strategy: "combined",
    legs: [
      {module:"spot",   instId:"BTC-USDT",               side:"buy", size_pct:50},
      {module:"option", instId:"BTC-USD-251226-90000-C",  side:"buy", size_pct:10}
    ],
    ...
    tool_calls: [
      {tool:"spot_place_order",   arguments:{instId:"BTC-USDT", side:"buy", ordType:"market", sz:"0.06"}},
      {tool:"option_place_order", arguments:{instId:"BTC-USD-251226-90000-C", side:"buy", ordType:"market", sz:"1"}}
    ]
  })

  NashGuard DRB 返回:
  {
    "approved": true,
    "max_drawdown_pct": 12.4,
    "var_95_pct": 8.1,
    "signature_hex": "a3f2c1d8e9f04b2c...",
    ...
  }

→ 调用 nashguard_execute(proposal, drb_result)

  NashGuard 内部:
  ✓ Ed25519 签名验证通过
  ✓ approved=true 确认
  → 调用 OKX Agent Trade Kit: spot_place_order → OK
  → 调用 OKX Agent Trade Kit: option_place_order → OK
```

**最终回复给用户：**
```
抱歉，全仓 100x 请求已被 NashGuard 安全层拦截
（该仓位将在 BTC 下跌 1% 即 $82,500 时爆仓）。

已自动调整为「现货50%+看涨期权10%」组合方案：
· 最大下行风险已锁定为 $1,240（本金的 12.4%）
· 上行收益理论无上限

已执行完毕。订单 Hash: 0xabc...
```

---

## 快速接入

### 1. 安装

```bash
cd nashguard
pip install -e .
# 若需要 OKX Agent Trade Kit：
npm install -g okx-trade-mcp
```

### 2. 配置

```bash
cp .env.example .env
# 只需填 OKX 凭证（可选，无凭证自动用模拟数据）：
# OKX_API_KEY=...
# OKX_SECRET_KEY=...
# OKX_PASSPHRASE=...
# OKX_DEMO=true   ← 默认模拟盘，安全
```

NashGuard **不需要填写任何 LLM API Key**。

### 3. 注册到你的 AI 客户端

**Claude Desktop / Claude Code：**
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

**Cursor / Windsurf：**
在 `.mcp.json` 中加入同样配置。

**任何支持 MCP 的 Agent：**
```bash
nashguard-mcp --demo    # stdio transport，标准 MCP 协议
```

---

## 风险参数配置

所有硬性限制在 `.env` 中调整（无需修改代码）：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `NASHGUARD_MAX_DRAWDOWN_PCT` | 20% | 超出即硬拒绝 |
| `NASHGUARD_MAX_LEVERAGE` | 10x | 单腿杠杆上限 |
| `NASHGUARD_MAX_POSITION_PCT` | 80% | 单笔最大占余额比例 |
| `NASHGUARD_MONTE_CARLO_SIMS` | 10,000 | GBM 模拟路径数 |
| `OKX_DEMO` | true | false = 实盘（需 OKX 凭证）|

---

## 单元测试（无需 API Key）

```bash
python -m pytest tests/ -v
```

所有测试为纯确定性数学验证，零网络请求，零 LLM 调用：
- Black-Scholes 看涨/看跌期权平价关系
- IV 隐含波动率 Newton-Raphson 求解精度
- 蒙特卡洛可复现性（固定随机种子）
- DRB：安全现货买入 → 通过；100x 杠杆 → 拒绝
- Ed25519 签名验证 + 篡改检测

---

## 与 OKX Agent Trade Kit 的结合

NashGuard 是 OKX Agent Trade Kit 的**安全执行层**：

| OKX 模块 | NashGuard 如何使用 |
|----------|------------------|
| `market` | `nashguard_snapshot`：获取实时价格锁存为 DRB 定价基准 |
| `account` | `nashguard_snapshot`：获取可用余额，防止 Agent 捏造数字 |
| `spot` | `nashguard_execute`：执行签名通过的现货腿 |
| `swap` | `nashguard_execute`：执行签名通过的永续合约腿 |
| `option` | `nashguard_execute`：执行签名通过的期权腿；`nashguard_validate`：Black-Scholes 定价验证 |
| `--demo` | 默认开启：所有执行先走模拟盘，`--live` 才切换实盘 |

---

## 为什么叫"纳什护盾"？

取自**约翰·纳什（Nash Equilibrium，纳什均衡）**。

当外部 Agent 用 NashGuard 进行多轮 Alpha/Risk/Judge 辩论时，博弈过程是在寻找纳什均衡：Alpha 想最大化收益，Risk 想最小化尾部风险，二者策略相互制衡，最终输出帕累托最优解。NashGuard 的数学沙盒则是这套均衡的客观裁量者——无论 Agent 怎么想，数学说了算。

---

## 开源协议

MIT License
