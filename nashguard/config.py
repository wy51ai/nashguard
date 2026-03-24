"""
NashGuard Configuration

Loads settings from environment variables and .env file.

── LLM Provider ─────────────────────────────────────────────────────────────
NashGuard uses litellm, which supports any provider.  Set model names in the
NASHGUARD_*_MODEL env vars using litellm's naming convention:

  Provider         Env key              Example model string
  ────────         ───────              ────────────────────────────────────
  Anthropic        ANTHROPIC_API_KEY    claude-haiku-4-5-20251001
  OpenAI           OPENAI_API_KEY       gpt-4o-mini
  Google Gemini    GEMINI_API_KEY       gemini/gemini-2.0-flash
  Mistral          MISTRAL_API_KEY      mistral/mistral-small-latest
  Ollama (local)   (none needed)        ollama/llama3.2
  DeepSeek         DEEPSEEK_API_KEY     deepseek/deepseek-chat

litellm reads the API key env vars automatically — you only need to set the
NASHGUARD_*_MODEL vars to switch providers.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _default_mcp_cmd() -> list[str]:
    """Locate the okx-trade-mcp binary or fall back to the local build."""
    binary = shutil.which("okx-trade-mcp")
    if binary:
        return [binary]

    # Try the local monorepo build
    local_index = (
        Path(__file__).parent.parent.parent
        / "agent-trade-kit"
        / "packages"
        / "mcp"
        / "dist"
        / "index.js"
    )
    if local_index.exists():
        node = shutil.which("node") or "node"
        return [node, str(local_index)]

    return ["okx-trade-mcp"]


@dataclass
class RiskParams:
    """Hard risk limits enforced by both MoA Risk agent and DRB sandbox."""

    max_drawdown_pct: float = 20.0
    max_leverage: float = 10.0
    max_position_pct: float = 80.0
    max_debate_rounds: int = 3
    monte_carlo_simulations: int = 10_000
    risk_free_rate: float = 0.05
    default_iv: float = 0.80


@dataclass
class Config:
    # ── LLM Model Selection ───────────────────────────────────────────────────
    # These are litellm model strings — change to any supported provider/model.
    # Default: Anthropic Claude (fast Haiku for Alpha, smarter Sonnet for Risk/Judge)
    alpha_model: str = field(
        default_factory=lambda: os.getenv(
            "NASHGUARD_ALPHA_MODEL", "claude-haiku-4-5-20251001"
        )
    )
    risk_model: str = field(
        default_factory=lambda: os.getenv(
            "NASHGUARD_RISK_MODEL", "claude-sonnet-4-6"
        )
    )
    judge_model: str = field(
        default_factory=lambda: os.getenv(
            "NASHGUARD_JUDGE_MODEL", "claude-sonnet-4-6"
        )
    )

    # ── Provider API Keys ─────────────────────────────────────────────────────
    # litellm reads these from environment automatically.
    # You can also pass them explicitly below; leave empty to use env vars.
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    gemini_api_key: str = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )

    # ── OKX ──────────────────────────────────────────────────────────────────
    okx_api_key: str = field(default_factory=lambda: os.getenv("OKX_API_KEY", ""))
    okx_secret_key: str = field(default_factory=lambda: os.getenv("OKX_SECRET_KEY", ""))
    okx_passphrase: str = field(default_factory=lambda: os.getenv("OKX_PASSPHRASE", ""))
    demo_mode: bool = field(
        default_factory=lambda: os.getenv("OKX_DEMO", "true").lower() != "false"
    )

    # ── MCP Server ───────────────────────────────────────────────────────────
    mcp_server_cmd: list[str] = field(default_factory=_default_mcp_cmd)

    # ── DRB Ed25519 signing key ───────────────────────────────────────────────
    key_store_path: str = field(
        default_factory=lambda: str(Path.home() / ".nashguard" / "signing_key.pem")
    )

    # ── Risk parameters ───────────────────────────────────────────────────────
    risk_params: RiskParams = field(default_factory=RiskParams)

    @property
    def has_llm_key(self) -> bool:
        """True if at least one LLM provider API key is configured."""
        # Ollama is local — no key needed.  If any model starts with "ollama/" return True.
        if any(
            m.startswith("ollama/")
            for m in (self.alpha_model, self.risk_model, self.judge_model)
        ):
            return True
        return bool(
            self.anthropic_api_key
            or self.openai_api_key
            or self.gemini_api_key
            or os.getenv("MISTRAL_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
        )

    @property
    def has_okx_credentials(self) -> bool:
        return bool(self.okx_api_key and self.okx_secret_key and self.okx_passphrase)

    # Keep for backwards compat (orchestrator still references this)
    @property
    def has_anthropic_key(self) -> bool:
        return self.has_llm_key
