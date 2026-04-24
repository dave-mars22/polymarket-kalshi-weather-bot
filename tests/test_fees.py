"""Tests for backend.core.fees.

Regression tests for the sign-convention fix: net_edge() must always
subtract fee_as_edge regardless of the sign of raw. An earlier version
had a sign-flip branch for raw <= 0 that produced wrong magnitudes
(with the wrong sign) when fees exceeded |raw|.
"""
from __future__ import annotations

import unittest

from backend.core.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    get_fee_model,
    net_edge,
)


class TestNetEdgeSignConvention(unittest.TestCase):
    def test_fees_always_reduce_net_regardless_of_sign(self):
        """Deep-OTM Kalshi case: fee_as_edge > |raw|, net must stay negative."""
        # At $0.01 entry and $6 trial size, Kalshi's per-contract ceiling
        # makes fee_as_edge ~100% of notional. A slightly-negative raw must
        # NOT flip to positive net under the new (correct) math.
        raw, net, fees = net_edge(
            model_prob=0.001, market_prob=0.010,
            entry_price=0.010, size_usd=6.0,
            venue="kalshi", market_type="monte_carlo",
        )
        fee_as_edge = fees.total / 6.0
        self.assertLess(raw, 0, f"raw must be negative: {raw}")
        self.assertGreater(
            fee_as_edge, abs(raw),
            f"test setup requires fee_as_edge ({fee_as_edge}) > |raw| ({abs(raw)})",
        )
        self.assertLess(net, 0, f"net must stay negative: raw={raw} net={net}")
        # net == raw - fee_as_edge, exactly
        self.assertAlmostEqual(net, raw - fee_as_edge, places=9)

    def test_typical_btc_positive_raw(self):
        """raw > 0: net = raw - fees."""
        raw, net, fees = net_edge(
            model_prob=0.55, market_prob=0.50,
            entry_price=0.55, size_usd=10.0,
            venue="polymarket", market_type="btc",
        )
        self.assertAlmostEqual(raw, 0.05, places=9)
        fee_as_edge = fees.total / 10.0
        self.assertAlmostEqual(net, raw - fee_as_edge, places=9)
        self.assertLess(net, raw)

    def test_typical_btc_negative_raw(self):
        """raw < 0: net should be MORE negative (not closer to zero)."""
        raw, net, fees = net_edge(
            model_prob=0.48, market_prob=0.52,
            entry_price=0.48, size_usd=10.0,
            venue="polymarket", market_type="btc",
        )
        self.assertAlmostEqual(raw, -0.04, places=9)
        fee_as_edge = fees.total / 10.0
        self.assertAlmostEqual(net, raw - fee_as_edge, places=9)
        self.assertLess(net, raw, "fees must push net strictly below raw")

    def test_zero_size_means_zero_fee_as_edge(self):
        """Edge case: size_usd == 0 => fee_as_edge is 0, net == raw."""
        raw, net, fees = net_edge(
            model_prob=0.55, market_prob=0.50,
            entry_price=0.55, size_usd=0.0,
            venue="polymarket", market_type="btc",
        )
        self.assertEqual(net, raw)


class TestFeeModels(unittest.TestCase):
    """Spot checks on the fee-model internals (not regression tests)."""

    def test_kalshi_per_contract_fee_peaks_at_0_50(self):
        model = KalshiFeeModel(slippage_bps=0)
        # Per-contract fee: ceil(0.07 * p * (1-p) * 100) / 100
        # p=0.50 => 0.07 * 0.25 = 0.0175 -> ceil(1.75)/100 = $0.02/contract
        self.assertAlmostEqual(model._per_contract_fee(0.50), 0.02)
        # p=0.01 => 0.07 * 0.0099 = 0.000693 -> ceil(0.0693)/100 = $0.01
        self.assertAlmostEqual(model._per_contract_fee(0.01), 0.01)

    def test_polymarket_has_gas_allowance(self):
        model = PolymarketFeeModel()
        breakdown = model.estimate_entry_cost(entry_price=0.50, size_usd=10.0)
        self.assertGreater(breakdown.gas, 0, "Polymarket should charge gas")

    def test_get_fee_model_dispatches_by_venue(self):
        self.assertIsInstance(
            get_fee_model("kalshi", market_type="monte_carlo"),
            KalshiFeeModel,
        )
        self.assertIsInstance(
            get_fee_model("polymarket", market_type="btc"),
            PolymarketFeeModel,
        )


class TestMarketTypeRouting(unittest.TestCase):
    """Explicit routing of market_type -> slippage band."""

    def test_btc_uses_btc_slippage(self):
        model = get_fee_model(
            "polymarket",
            btc_slippage_bps=10, kalshi_slippage_bps=50,
            market_type="btc",
        )
        self.assertIsInstance(model, PolymarketFeeModel)
        self.assertEqual(model.slippage_bps, 10)

    def test_monte_carlo_uses_kalshi_slippage(self):
        model = get_fee_model(
            "kalshi",
            btc_slippage_bps=10, kalshi_slippage_bps=50,
            market_type="monte_carlo",
        )
        self.assertIsInstance(model, KalshiFeeModel)
        self.assertEqual(model.slippage_bps, 50)

    def test_unknown_market_type_raises(self):
        """Silent fall-through on unknown market_type would hide bugs."""
        for bad in ("weather", "mc", "monte-carlo", "", "BTC"):
            with self.assertRaises(ValueError, msg=f"market_type={bad!r} should raise"):
                get_fee_model("kalshi", market_type=bad)

    def test_unknown_venue_raises(self):
        with self.assertRaises(ValueError):
            get_fee_model("dydx", market_type="btc")  # noqa: type-check would catch


if __name__ == "__main__":
    unittest.main()
