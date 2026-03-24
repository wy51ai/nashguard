"""
Unit tests for the DRB sandbox components.

These tests verify the mathematical correctness of Black-Scholes,
Monte Carlo simulation, and DRB signing — independently of any LLM or API.
"""

import json
import math
import unittest

from nashguard.sandbox.black_scholes import BSInputs, price as bs_price, implied_volatility
from nashguard.sandbox.monte_carlo import SimLeg, simulate
from nashguard.config import Config, RiskParams
from nashguard.models import MarketSnapshot, AccountSnapshot, TradeProposal, Leg, Strategy
from nashguard.sandbox.drb import DRBSandbox


class TestBlackScholes(unittest.TestCase):
    def test_call_put_parity(self):
        """Put-call parity: C - P = S - K*exp(-rT)"""
        S, K, T, r, sigma = 65000.0, 70000.0, 0.25, 0.05, 0.80
        call = bs_price(BSInputs(S=S, K=K, T=T, r=r, sigma=sigma, option_type="call"))
        put = bs_price(BSInputs(S=S, K=K, T=T, r=r, sigma=sigma, option_type="put"))
        parity_lhs = call.price - put.price
        parity_rhs = S - K * math.exp(-r * T)
        self.assertAlmostEqual(parity_lhs, parity_rhs, places=4)

    def test_deep_itm_call_approaches_intrinsic(self):
        """Deep ITM call should approach intrinsic value (S - K)."""
        S, K = 100_000.0, 50_000.0
        result = bs_price(BSInputs(S=S, K=K, T=0.001, r=0.05, sigma=0.80, option_type="call"))
        intrinsic = S - K
        self.assertAlmostEqual(result.price, intrinsic, delta=200.0)

    def test_delta_range(self):
        """Option delta must be between 0 and 1 for calls."""
        result = bs_price(BSInputs(S=65000, K=70000, T=0.25, r=0.05, sigma=0.80, option_type="call"))
        self.assertGreater(result.delta, 0.0)
        self.assertLess(result.delta, 1.0)

    def test_expired_option_zero_time(self):
        """At expiry (T=0), call with S>K should return intrinsic."""
        result = bs_price(BSInputs(S=70000, K=65000, T=0.0, r=0.05, sigma=0.80, option_type="call"))
        self.assertAlmostEqual(result.price, 5000.0, delta=0.01)

    def test_implied_vol_round_trip(self):
        """IV solver should recover the original sigma."""
        sigma_true = 0.75
        inputs = BSInputs(S=65000, K=65000, T=0.25, r=0.05, sigma=sigma_true, option_type="call")
        market_price = bs_price(inputs).price
        iv = implied_volatility(market_price, S=65000, K=65000, T=0.25, r=0.05, option_type="call")
        self.assertAlmostEqual(iv, sigma_true, places=3)


class TestMonteCarlo(unittest.TestCase):
    def test_unleveraged_spot_var_positive(self):
        """VaR for an unleveraged spot position must be >= 0."""
        legs = [SimLeg("BTC-USDT", entry_price=65000, notional_usdt=1000, leverage=1.0, is_long=True)]
        result = simulate(legs, spot_price=65000, annual_vol=0.80, n_sims=1000)
        self.assertGreaterEqual(result.var_95_pct, 0.0)
        self.assertGreaterEqual(result.max_drawdown_pct, 0.0)

    def test_100x_leverage_high_risk(self):
        """100x leverage should produce very high VaR."""
        legs = [SimLeg("BTC-SWAP", entry_price=65000, notional_usdt=1000, leverage=100.0, is_long=True)]
        result = simulate(legs, spot_price=65000, annual_vol=0.80, n_sims=2000)
        # 100x leverage with 80% annual vol — VaR should be extremely high
        self.assertGreater(result.var_95_pct, 50.0)

    def test_short_position_liq_price(self):
        """Short position should have liquidation price above entry."""
        legs = [SimLeg("BTC-SWAP", entry_price=65000, notional_usdt=1000, leverage=10.0, is_long=False)]
        result = simulate(legs, spot_price=65000)
        if result.liquidation_price:
            self.assertGreater(result.liquidation_price, 65000)

    def test_reproducibility(self):
        """Same seed must produce same result."""
        legs = [SimLeg("BTC", entry_price=65000, notional_usdt=5000, leverage=2.0)]
        r1 = simulate(legs, spot_price=65000, seed=123)
        r2 = simulate(legs, spot_price=65000, seed=123)
        self.assertAlmostEqual(r1.var_95_pct, r2.var_95_pct, places=6)


class TestDRBSandbox(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmpdir = tempfile.mkdtemp()
        self.key_path = os.path.join(self.tmpdir, "test_key.pem")
        self.drb = DRBSandbox(RiskParams(), self.key_path)

    def _make_proposal(self, leverage=1.0, size_pct=50.0) -> TradeProposal:
        return TradeProposal(
            strategy=Strategy.SPOT_BUY,
            legs=[Leg(module="spot", instId="BTC-USDT", side="buy", size_pct=size_pct, leverage=leverage)],
            rationale="Test",
            expected_return_pct=5.0,
            risk_summary="Test",
        )

    def test_safe_spot_buy_approved(self):
        proposal = self._make_proposal(leverage=1.0, size_pct=30.0)
        market = MarketSnapshot(btc_price=65000, btc_24h_change_pct=1.5)
        account = AccountSnapshot(total_eq_usdt=10000, available_eq_usdt=10000)
        result = self.drb.evaluate(proposal, market, account)
        self.assertTrue(result.approved)

    def test_excessive_leverage_rejected(self):
        proposal = self._make_proposal(leverage=100.0, size_pct=50.0)
        market = MarketSnapshot(btc_price=65000, btc_24h_change_pct=1.5)
        account = AccountSnapshot(total_eq_usdt=10000, available_eq_usdt=10000)
        result = self.drb.evaluate(proposal, market, account)
        self.assertFalse(result.approved)
        self.assertIn("Leverage", result.rejection_reason or "")

    def test_signature_verifiable(self):
        proposal = self._make_proposal()
        market = MarketSnapshot(btc_price=65000, btc_24h_change_pct=0.5)
        account = AccountSnapshot(total_eq_usdt=10000, available_eq_usdt=10000)
        result = self.drb.evaluate(proposal, market, account)
        self.assertTrue(self.drb.verify(result))

    def test_tampered_result_fails_verification(self):
        proposal = self._make_proposal()
        market = MarketSnapshot(btc_price=65000, btc_24h_change_pct=0.5)
        account = AccountSnapshot(total_eq_usdt=10000, available_eq_usdt=10000)
        result = self.drb.evaluate(proposal, market, account)
        # Tamper with the result
        result.approved = not result.approved
        self.assertFalse(self.drb.verify(result))


if __name__ == "__main__":
    unittest.main()
