"""
OKX MCP Client

Connects to the OKX Agent Trade Kit MCP server via subprocess stdio transport.
Speaks JSON-RPC 2.0 — each message is a newline-terminated JSON object.

Handshake sequence:
  1. Spawn the okx-trade-mcp process
  2. Send  → initialize (with client capabilities)
  3. Receive ← initialize result (server capabilities)
  4. Send  → notifications/initialized
  5. Ready to call tools

Usage:
    async with OKXMCPClient(config) as client:
        ticker = await client.call_tool("market_get_ticker", {"instId": "BTC-USDT"})
        balance = await client.call_tool("account_get_balance", {})
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models import AccountSnapshot, MarketSnapshot

log = logging.getLogger(__name__)


class MCPError(Exception):
    pass


class OKXMCPClient:
    """
    Async subprocess MCP client for OKX Agent Trade Kit.

    Manages the server process lifetime and provides a simple call_tool() API.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    # ─── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "OKXMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start the MCP server process and perform the MCP handshake."""
        cmd = self.config.mcp_server_cmd[:]

        # Add modules
        cmd += ["--modules", "market,spot,swap,option,account"]

        # Add demo flag if required
        if self.config.demo_mode:
            cmd.append("--demo")

        # Build environment
        env = os.environ.copy()
        if self.config.has_okx_credentials:
            env["OKX_API_KEY"] = self.config.okx_api_key
            env["OKX_SECRET_KEY"] = self.config.okx_secret_key
            env["OKX_PASSPHRASE"] = self.config.okx_passphrase

        log.info("[MCP] Starting OKX MCP server: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Start background reader
        self._reader_task = asyncio.create_task(self._reader_loop())

        # MCP handshake
        await self._initialize()
        log.info("[MCP] OKX Agent Trade Kit connected (demo=%s)", self.config.demo_mode)

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                pass
        log.info("[MCP] OKX MCP server disconnected")

    # ─── Tool calls ──────────────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call an OKX Agent Trade Kit tool and return the parsed result."""
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        # MCP tool results are in result["content"][0]["text"]
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            text = content[0]["text"]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        return result

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List all available tools from the MCP server."""
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    # ─── High-level helpers ──────────────────────────────────────────────────

    async def get_market_snapshot(self) -> MarketSnapshot:
        """Fetch BTC ticker, funding rate, and ETH ticker."""
        btc_ticker = await self.call_tool("market_get_ticker", {"instId": "BTC-USDT"})
        btc_data = btc_ticker.get("data", [{}])[0] if isinstance(btc_ticker.get("data"), list) else {}

        btc_price = float(btc_data.get("last", 0) or 0)
        btc_open = float(btc_data.get("open24h", btc_price) or btc_price)
        btc_change = ((btc_price - btc_open) / btc_open * 100) if btc_open else 0.0

        funding_rate = None
        try:
            fr_data = await self.call_tool(
                "market_get_funding_rate", {"instId": "BTC-USDT-SWAP"}
            )
            fr_list = fr_data.get("data", [])
            if fr_list:
                funding_rate = float(fr_list[0].get("fundingRate", 0))
        except Exception:
            pass

        eth_price = None
        try:
            eth_ticker = await self.call_tool("market_get_ticker", {"instId": "ETH-USDT"})
            eth_data = eth_ticker.get("data", [{}])[0] if isinstance(eth_ticker.get("data"), list) else {}
            eth_price = float(eth_data.get("last", 0) or 0) or None
        except Exception:
            pass

        return MarketSnapshot(
            btc_price=btc_price,
            btc_24h_change_pct=btc_change,
            btc_funding_rate=funding_rate,
            eth_price=eth_price,
        )

    async def get_account_snapshot(self) -> AccountSnapshot:
        """Fetch account balance and open positions."""
        if not self.config.has_okx_credentials:
            log.warning("[MCP] No OKX credentials — using mock account data")
            return AccountSnapshot(
                total_eq_usdt=10_000.0,
                available_eq_usdt=10_000.0,
                has_credentials=False,
            )

        balance = await self.call_tool("account_get_balance", {})
        details = balance.get("data", [{}])[0] if isinstance(balance.get("data"), list) else {}
        total_eq = float(details.get("totalEq", 0) or 0)
        avail_eq = float(details.get("adjEq", total_eq) or total_eq)

        positions = []
        try:
            pos_data = await self.call_tool("swap_get_positions", {})
            positions = pos_data.get("data", [])
        except Exception:
            pass

        return AccountSnapshot(
            total_eq_usdt=total_eq,
            available_eq_usdt=avail_eq,
            positions=positions,
            has_credentials=True,
        )

    async def execute_leg(
        self, module: str, tool: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a single trade leg via the MCP server."""
        log.info("[MCP] Executing %s.%s(%s)", module, tool, arguments)
        return await self.call_tool(tool, arguments)

    # ─── JSON-RPC internals ───────────────────────────────────────────────────

    async def _initialize(self) -> None:
        """Perform the MCP initialize handshake."""
        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "nashguard", "version": "1.0.0"},
            },
        )
        log.debug("[MCP] Server info: %s", result.get("serverInfo", {}))

        # Send initialized notification (no response expected)
        await self._notify("notifications/initialized", {})

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request and await the response."""
        self._request_id += 1
        rid = self._request_id

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[rid] = future

        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": rid})
        await self._send(msg)

        try:
            result = await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise MCPError(f"Timeout waiting for response to {method}")

        if "error" in result:
            raise MCPError(f"MCP error: {result['error']}")
        return result.get("result", result)

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        await self._send(msg)

    async def _send(self, message: str) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPError("MCP server not running")
        data = (message + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        """Background task: read lines from the MCP server stdout and dispatch."""
        if not self._proc or not self._proc.stdout:
            return
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("[MCP] Non-JSON stdout: %s", line[:200])
                    continue

                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    fut = self._pending.pop(rid)
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("[MCP] Reader loop error: %s", e)
            # Fail all pending requests
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MCPError(f"Reader loop died: {e}"))
            self._pending.clear()
