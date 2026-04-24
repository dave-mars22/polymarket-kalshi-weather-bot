"""Tests for backend.core.monte_carlo.

Strategy: compare Monte Carlo prob_above against the closed-form lognormal
CDF (prob_above_analytic). For a correct simulator these must agree within
the binomial standard error ~1/sqrt(n_paths).

We also verify determinism under seed, probability axioms, the martingale
property E[S_T] = S_0 * exp(mu*T), and edge-case error handling.
"""
from __future__ import annotations

import math
import unittest

import numpy as np

from backend.core.monte_carlo import (
    prob_above_analytic,
    simulate_terminal_prices,
)


def _mc_tolerance(p: float, n: int, sigmas: float = 4.0) -> float:
    """~sigma bound for a proportion MC estimate. sigmas=4 ~ 6-sigma confidence."""
    return sigmas * math.sqrt(max(p * (1 - p), 1e-6) / n)


class TestAnalyticOracle(unittest.TestCase):
    """Sanity-check prob_above_analytic itself against known values."""

    def test_prob_at_spot_with_zero_drift(self):
        # With mu=0, P(S_T > S_0) = Phi(-sigma*sqrt(T)/2), strictly < 0.5
        # because of the vol drag in the drift correction.
        p = prob_above_analytic(
            spot=100.0, threshold=100.0, drift_annual=0.0,
            vol_annual=0.30, years_to_expiry=1.0,
        )
        expected = 0.5 * (1.0 + math.erf(-0.15 / math.sqrt(2.0)))  # Phi(-0.15)
        self.assertAlmostEqual(p, expected, places=10)
        self.assertLess(p, 0.5)

    def test_deep_itm_tends_to_one(self):
        p = prob_above_analytic(
            spot=100.0, threshold=1.0, drift_annual=0.05,
            vol_annual=0.20, years_to_expiry=1.0,
        )
        self.assertGreater(p, 0.999)

    def test_deep_otm_tends_to_zero(self):
        p = prob_above_analytic(
            spot=100.0, threshold=10_000.0, drift_annual=0.05,
            vol_annual=0.20, years_to_expiry=1.0,
        )
        self.assertLess(p, 0.001)


class TestMonteCarloConvergence(unittest.TestCase):
    """MC prob_above must converge to the analytic oracle."""

    N = 200_000

    def _compare(self, spot, threshold, mu, sigma, T):
        analytic = prob_above_analytic(spot, threshold, mu, sigma, T)
        sim = simulate_terminal_prices(
            spot=spot, drift_annual=mu, vol_annual=sigma,
            years_to_expiry=T, n_paths=self.N, seed=42,
        )
        mc = sim.prob_above(threshold)
        tol = _mc_tolerance(analytic, self.N)
        self.assertLess(
            abs(mc - analytic), tol,
            msg=f"MC={mc:.5f} vs analytic={analytic:.5f}, "
                f"diff={abs(mc-analytic):.5f} >= tol={tol:.5f}",
        )

    def test_atm_one_year(self):
        self._compare(100.0, 100.0, 0.05, 0.20, 1.0)

    def test_itm_three_month(self):
        self._compare(100.0, 90.0, 0.0, 0.30, 0.25)

    def test_otm_three_month(self):
        self._compare(100.0, 120.0, 0.0, 0.30, 0.25)

    def test_btc_realistic(self):
        # 80k spot, 100k threshold, 7 days out, 70% annualized vol.
        self._compare(80_000.0, 100_000.0, 0.0, 0.70, 7 / 365.25)

    def test_spx_realistic(self):
        # 5800 spot, 6000 threshold, 30 days out, 15% vol, 6% drift.
        self._compare(5800.0, 6000.0, 0.06, 0.15, 30 / 365.25)

    def test_high_vol_crypto_long_horizon(self):
        self._compare(50_000.0, 50_000.0, 0.0, 1.00, 30 / 365.25)


class TestProbabilityAxioms(unittest.TestCase):

    def test_above_plus_below_is_one(self):
        sim = simulate_terminal_prices(
            spot=100.0, drift_annual=0.05, vol_annual=0.25,
            years_to_expiry=0.5, n_paths=10_000, seed=1,
        )
        p_above = sim.prob_above(110.0)
        p_below = sim.prob_below(110.0)
        self.assertAlmostEqual(p_above + p_below, 1.0, places=12)

    def test_prob_in_range_partition(self):
        sim = simulate_terminal_prices(
            spot=100.0, drift_annual=0.0, vol_annual=0.30,
            years_to_expiry=1.0, n_paths=50_000, seed=2,
        )
        L, H = 90.0, 120.0
        p_below_L = sim.prob_below(L)
        p_in = sim.prob_in_range(L, H)
        p_above_H = sim.prob_above(H)
        # Partition should sum to 1 within float tolerance.
        total = p_below_L + p_in + p_above_H
        self.assertAlmostEqual(total, 1.0, places=12)

    def test_range_low_greater_than_high_raises(self):
        sim = simulate_terminal_prices(
            spot=100.0, drift_annual=0.0, vol_annual=0.2,
            years_to_expiry=1.0, n_paths=100, seed=3,
        )
        with self.assertRaises(ValueError):
            sim.prob_in_range(120.0, 110.0)


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_output(self):
        a = simulate_terminal_prices(100, 0.05, 0.2, 1.0, n_paths=500, seed=7)
        b = simulate_terminal_prices(100, 0.05, 0.2, 1.0, n_paths=500, seed=7)
        np.testing.assert_array_equal(a.terminal_prices, b.terminal_prices)

    def test_different_seeds_differ(self):
        a = simulate_terminal_prices(100, 0.05, 0.2, 1.0, n_paths=500, seed=7)
        b = simulate_terminal_prices(100, 0.05, 0.2, 1.0, n_paths=500, seed=8)
        self.assertFalse(np.array_equal(a.terminal_prices, b.terminal_prices))


class TestMartingaleProperty(unittest.TestCase):
    """Under GBM, E[S_T] = S_0 * exp(mu*T). MC mean must converge to this."""

    def test_expected_value(self):
        S0, mu, sigma, T = 100.0, 0.07, 0.25, 1.0
        n = 500_000
        sim = simulate_terminal_prices(S0, mu, sigma, T, n_paths=n, seed=10)
        expected = S0 * math.exp(mu * T)
        actual = float(sim.terminal_prices.mean())
        # SE of the mean for lognormal: S0 * exp(mu*T) * sqrt(exp(sigma^2*T) - 1) / sqrt(n)
        se = S0 * math.exp(mu * T) * math.sqrt(math.exp(sigma ** 2 * T) - 1) / math.sqrt(n)
        self.assertLess(abs(actual - expected), 4 * se)


class TestInputValidation(unittest.TestCase):
    def test_non_positive_spot_raises(self):
        for bad in (0, -1, -100.0):
            with self.assertRaises(ValueError):
                simulate_terminal_prices(bad, 0.05, 0.2, 1.0, n_paths=10)

    def test_negative_vol_raises(self):
        with self.assertRaises(ValueError):
            simulate_terminal_prices(100, 0.05, -0.1, 1.0, n_paths=10)

    def test_non_positive_T_raises(self):
        for bad in (0, -0.1):
            with self.assertRaises(ValueError):
                simulate_terminal_prices(100, 0.05, 0.2, bad, n_paths=10)

    def test_zero_vol_is_valid(self):
        """sigma=0 degenerates to S_T = S_0 * exp(mu*T) with zero variance."""
        sim = simulate_terminal_prices(100, 0.05, 0.0, 1.0, n_paths=10, seed=0)
        expected = 100 * math.exp(0.05)
        np.testing.assert_allclose(sim.terminal_prices, expected)


if __name__ == "__main__":
    unittest.main()
