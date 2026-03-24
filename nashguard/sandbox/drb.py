"""
DRB — Deterministic Risk Bounding Sandbox

This is the non-LLM mathematical firewall of NashGuard.

Pipeline:
  1. Receive a JudgeDecision (trade plan)
  2. Build SimLeg objects from the trade legs
  3. Run Monte Carlo simulation
  4. If options are involved, price them with Black-Scholes
  5. Compute: Max Drawdown, VaR95, CVaR, Liquidation Price
  6. Compare against user's RiskParams hard limits
  7. Sign the result with a local Ed25519 key
  8. Return DRBResult — if approved=False, execution is hard-blocked

The Ed25519 signature means the OKX execution layer can cryptographically
verify that the DRB sandbox actually ran and approved the trade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

from ..config import RiskParams
from ..models import AccountSnapshot, DRBResult, JudgeDecision, MarketSnapshot, TradeProposal
from .black_scholes import BSInputs, price as bs_price
from .monte_carlo import SimLeg, simulate

log = logging.getLogger(__name__)


class DRBSandbox:
    """
    Deterministic Risk Bounding sandbox.

    Maintains a local Ed25519 key pair for signing approved risk assessments.
    The private key lives in ~/.nashguard/signing_key.pem and never leaves the
    local machine.
    """

    def __init__(self, risk_params: RiskParams, key_store_path: str) -> None:
        self.risk_params = risk_params
        self.key_store_path = Path(key_store_path)
        self._private_key = self._load_or_generate_key()

    # ─── Public API ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        proposal: TradeProposal,
        market: MarketSnapshot,
        account: AccountSnapshot,
    ) -> DRBResult:
        """
        Full DRB evaluation pipeline. Returns a signed DRBResult.
        If approved=False the execution layer must hard-block the trade.
        """
        log.info("[DRB] Starting deterministic risk evaluation")

        available = account.available_eq_usdt
        spot_price = market.btc_price

        # Build simulation legs
        sim_legs = self._build_sim_legs(proposal, available, spot_price, market)

        # Run Monte Carlo — express all P&L as % of available_usdt (full portfolio)
        mc = simulate(
            legs=sim_legs,
            spot_price=spot_price,
            annual_vol=self.risk_params.default_iv,
            n_sims=self.risk_params.monte_carlo_simulations,
            total_portfolio_usdt=available,
        )

        # Option Greeks (if any option legs present)
        option_details: dict = {}
        for leg in proposal.legs:
            if leg.module == "option" and leg.instId:
                bs = self._price_option_leg(leg, market)
                if bs:
                    option_details[leg.instId] = bs

        # Determine approval
        rejection_reason: Optional[str] = None

        if mc.max_drawdown_pct > self.risk_params.max_drawdown_pct:
            rejection_reason = (
                f"Max drawdown {mc.max_drawdown_pct:.1f}% exceeds hard limit "
                f"{self.risk_params.max_drawdown_pct:.1f}%"
            )

        for leg in proposal.legs:
            if leg.leverage > self.risk_params.max_leverage:
                rejection_reason = (
                    f"Leverage {leg.leverage}x on {leg.instId} exceeds "
                    f"hard limit {self.risk_params.max_leverage}x"
                )

            if leg.size_pct > self.risk_params.max_position_pct:
                rejection_reason = (
                    f"Position size {leg.size_pct:.0f}% on {leg.instId} exceeds "
                    f"hard limit {self.risk_params.max_position_pct:.0f}%"
                )

        approved = rejection_reason is None

        # Build the payload to sign
        payload = {
            "approved": approved,
            "max_drawdown_pct": round(mc.max_drawdown_pct, 4),
            "var_95_pct": round(mc.var_95_pct, 4),
            "expected_shortfall_pct": round(mc.expected_shortfall_pct, 4),
            "liquidation_price": mc.liquidation_price,
            "rejection_reason": rejection_reason,
        }
        signature_hex = self._sign(json.dumps(payload, sort_keys=True))

        log.info(
            "[DRB] Result: approved=%s, max_dd=%.1f%%, VaR95=%.1f%%",
            approved,
            mc.max_drawdown_pct,
            mc.var_95_pct,
        )

        return DRBResult(
            max_drawdown_pct=mc.max_drawdown_pct,
            liquidation_price=mc.liquidation_price,
            var_95_pct=mc.var_95_pct,
            expected_shortfall_pct=mc.expected_shortfall_pct,
            approved=approved,
            signature_hex=signature_hex,
            rejection_reason=rejection_reason,
            details={
                "worst_case_usdt": round(mc.worst_case_usdt, 2),
                "best_case_usdt": round(mc.best_case_usdt, 2),
                "n_simulations": self.risk_params.monte_carlo_simulations,
                "option_greeks": option_details,
                "public_key_hex": self.public_key_hex,
            },
        )

    def verify(self, drb_result: DRBResult) -> bool:
        """Verify the Ed25519 signature on a DRBResult (called by execution layer)."""
        payload = {
            "approved": drb_result.approved,
            "max_drawdown_pct": round(drb_result.max_drawdown_pct, 4),
            "var_95_pct": round(drb_result.var_95_pct, 4),
            "expected_shortfall_pct": round(drb_result.expected_shortfall_pct, 4),
            "liquidation_price": drb_result.liquidation_price,
            "rejection_reason": drb_result.rejection_reason,
        }
        try:
            pub = self._private_key.public_key()
            pub.verify(
                bytes.fromhex(drb_result.signature_hex),
                json.dumps(payload, sort_keys=True).encode(),
            )
            return True
        except Exception:
            return False

    @property
    def public_key_hex(self) -> str:
        pub_bytes = self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        return pub_bytes.hex()

    # ─── Private helpers ─────────────────────────────────────────────────────

    def _build_sim_legs(
        self,
        proposal: TradeProposal,
        available_usdt: float,
        spot_price: float,
        market: MarketSnapshot,
    ) -> list[SimLeg]:
        legs = []
        for leg in proposal.legs:
            # notional_usdt = margin committed (leverage applied separately in MC)
            margin = available_usdt * (leg.size_pct / 100.0)
            is_option = leg.module == "option"
            legs.append(
                SimLeg(
                    name=leg.instId,
                    entry_price=spot_price,
                    notional_usdt=margin,
                    leverage=leg.leverage,
                    is_long=leg.side == "buy",
                    is_option=is_option,
                )
            )
        return legs

    def _price_option_leg(self, leg, market: MarketSnapshot) -> Optional[dict]:
        """Attempt to price an option leg with Black-Scholes."""
        try:
            # Parse expiry/strike from OKX instId e.g. BTC-USD-240329-50000-C
            parts = leg.instId.split("-")
            if len(parts) < 5:
                return None
            strike = float(parts[3])
            option_type = "call" if parts[4].upper() == "C" else "put"

            # Approximate T from instId date YYMMDD
            import datetime
            expiry_str = parts[2]
            expiry = datetime.datetime.strptime("20" + expiry_str, "%Y%m%d")
            T = max((expiry - datetime.datetime.utcnow()).days / 365.0, 0.001)

            bs_in = BSInputs(
                S=market.btc_price,
                K=strike,
                T=T,
                r=0.05,
                sigma=self.risk_params.default_iv,
                option_type=option_type,
            )
            result = bs_price(bs_in)
            return {
                "fair_value": round(result.price, 4),
                "delta": round(result.delta, 4),
                "gamma": round(result.gamma, 6),
                "theta_per_day": round(result.theta, 4),
                "vega_per_1pct_iv": round(result.vega, 4),
            }
        except Exception as e:
            log.warning("[DRB] Option pricing failed for %s: %s", leg.instId, e)
            return None

    def _sign(self, message: str) -> str:
        sig = self._private_key.sign(message.encode())
        return sig.hex()

    def _load_or_generate_key(self) -> Ed25519PrivateKey:
        """Load existing Ed25519 private key or generate and persist a new one."""
        self.key_store_path.parent.mkdir(parents=True, exist_ok=True)

        if self.key_store_path.exists():
            pem = self.key_store_path.read_bytes()
            key = load_pem_private_key(pem, password=None)
            log.debug("[DRB] Loaded signing key from %s", self.key_store_path)
            return key  # type: ignore[return-value]

        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        self.key_store_path.write_bytes(pem)
        # Restrict permissions to owner-only
        os.chmod(self.key_store_path, 0o600)
        log.info("[DRB] Generated new Ed25519 signing key at %s", self.key_store_path)
        return key
