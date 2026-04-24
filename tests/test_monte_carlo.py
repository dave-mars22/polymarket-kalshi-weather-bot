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
    prob_one_touch_above_analytic,
    prob_one_touch_below_analytic,
    simulate_paths,
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


class TestOneTouchAnalytic(unittest.TestCase):
    """Closed-form reflection-principle sanity checks."""

    def test_barrier_at_or_below_spot_returns_one_upside(self):
        """Upside barrier B <= S_0 is already touched at t=0."""
        self.assertEqual(
            prob_one_touch_above_analytic(100.0, 100.0, 0.0, 0.3, 1.0), 1.0
        )
        self.assertEqual(
            prob_one_touch_above_analytic(100.0, 50.0, 0.0, 0.3, 1.0), 1.0
        )

    def test_barrier_at_or_above_spot_returns_one_downside(self):
        self.assertEqual(
            prob_one_touch_below_analytic(100.0, 100.0, 0.0, 0.3, 1.0), 1.0
        )
        self.assertEqual(
            prob_one_touch_below_analytic(100.0, 150.0, 0.0, 0.3, 1.0), 1.0
        )

    def test_reflection_principle_zero_drift_upside(self):
        """With mu = sigma^2/2 (so mu_tilde = 0), P(max B_t >= m) = 2*Phi(-m/(sigma*sqrt(T)))
        for pure BM. Here mu must be set so that mu_tilde = 0."""
        sigma = 0.2
        T = 1.0
        mu_for_zero_tilde = 0.5 * sigma ** 2
        # Barrier at +1 sigma*sqrt(T) above spot in log space:
        spot, barrier = 100.0, 100.0 * math.exp(sigma * math.sqrt(T))
        p = prob_one_touch_above_analytic(spot, barrier, mu_for_zero_tilde, sigma, T)
        # For zero drift in log space, P = 2 * Phi(-1) ≈ 0.3173
        expected = 2.0 * 0.5 * (1.0 + math.erf(-1.0 / math.sqrt(2.0)))
        self.assertAlmostEqual(p, expected, places=6)

    def test_one_touch_always_gte_european(self):
        """For the same barrier, P(max >= B) >= P(S_T >= B) always."""
        test_cases = [
            (100.0, 110.0, 0.05, 0.20, 0.25),
            (100.0, 105.0, 0.00, 0.30, 0.5),
            (100.0, 200.0, 0.10, 0.40, 2.0),
            (5800.0, 6000.0, 0.06, 0.15, 30 / 365.25),
            (80_000.0, 85_000.0, 0.0, 0.70, 7 / 365.25),
        ]
        for spot, barrier, mu, sigma, T in test_cases:
            p_ot = prob_one_touch_above_analytic(spot, barrier, mu, sigma, T)
            p_eu = prob_above_analytic(spot, barrier, mu, sigma, T)
            self.assertGreaterEqual(
                p_ot, p_eu - 1e-12,
                f"P(one_touch)={p_ot:.6f} < P(terminal)={p_eu:.6f} "
                f"for spot={spot}, B={barrier}, T={T}"
            )

    def test_positive_drift_long_horizon_approaches_one(self):
        """With positive drift and long T, P(ever hit B>S_0) -> 1."""
        p = prob_one_touch_above_analytic(100.0, 150.0, 0.20, 0.20, 20.0)
        self.assertGreater(p, 0.99)

    def test_negative_drift_long_horizon_bounded(self):
        """With negative drift, P(ever hit B>S_0) stays bounded away from 1
        and approaches exp(2*mu_tilde*log(B/S_0)/sigma^2)."""
        spot, B, mu, sigma, T = 100.0, 150.0, -0.15, 0.20, 20.0
        p = prob_one_touch_above_analytic(spot, B, mu, sigma, T)
        mu_tilde = mu - 0.5 * sigma ** 2
        limit = math.exp(2.0 * mu_tilde * math.log(B / spot) / sigma ** 2)
        # With T=20, we should be very close to the asymptotic limit.
        self.assertLess(abs(p - limit), 0.05, f"p={p}, limit={limit}")

    def test_upside_downside_symmetry_zero_drift(self):
        """With zero log-drift (mu = sigma^2/2), upside barrier at 2*S_0 gives
        same hit probability as downside barrier at S_0/2."""
        sigma = 0.25
        T = 1.0
        mu = 0.5 * sigma ** 2
        p_up = prob_one_touch_above_analytic(100.0, 200.0, mu, sigma, T)
        p_dn = prob_one_touch_below_analytic(100.0, 50.0, mu, sigma, T)
        # Under zero log-drift these are exactly equal.
        self.assertAlmostEqual(p_up, p_dn, places=6)

    def test_determinism(self):
        args = (100.0, 110.0, 0.05, 0.20, 0.5)
        self.assertEqual(
            prob_one_touch_above_analytic(*args),
            prob_one_touch_above_analytic(*args),
        )


class TestOneTouchMCVerification(unittest.TestCase):
    """Cross-check the closed-form against a Monte Carlo path simulation.

    MC under discrete sampling slightly underestimates the true hit
    probability (misses between-step crossings). With 200 time-steps and
    50k paths, the bias should be within ~2pp and MC <= analytic always.
    """

    def _mc_hit(self, spot, barrier, mu, sigma, T, n_paths=50_000, n_steps=200, seed=11):
        paths = simulate_paths(spot, mu, sigma, T, n_paths, n_steps, seed=seed)
        if barrier > spot:
            return float((paths.max(axis=1) >= barrier).mean())
        return float((paths.min(axis=1) <= barrier).mean())

    def _check(self, spot, barrier, mu, sigma, T, tol=0.025):
        analytic = (
            prob_one_touch_above_analytic(spot, barrier, mu, sigma, T)
            if barrier > spot
            else prob_one_touch_below_analytic(spot, barrier, mu, sigma, T)
        )
        mc = self._mc_hit(spot, barrier, mu, sigma, T)
        # MC <= analytic expected (discrete sampling misses crossings).
        # Tolerance allows the known bias.
        self.assertLessEqual(
            mc, analytic + 0.005,
            f"MC={mc:.4f} exceeds analytic={analytic:.4f} (should be <= analytic)",
        )
        self.assertLess(
            abs(mc - analytic), tol,
            f"MC={mc:.4f} vs analytic={analytic:.4f}, "
            f"diff={abs(mc-analytic):.4f} > tol={tol}",
        )

    def test_near_atm_upside(self):
        # Near-ATM barriers suffer more discretization bias (paths miss
        # between-step crossings). 4pp tolerance accounts for it.
        self._check(100.0, 105.0, 0.05, 0.25, 0.5, tol=0.04)

    def test_far_otm_upside_zero_drift(self):
        self._check(100.0, 130.0, 0.0, 0.25, 0.5)

    def test_downside_near(self):
        self._check(100.0, 95.0, 0.0, 0.25, 0.5, tol=0.04)

    def test_btc_realistic_upside_barrier(self):
        # BTC at 78k, barrier at 85k (~9% above), 7 days, 45% vol.
        self._check(78_000.0, 85_000.0, 0.0, 0.45, 7 / 365.25)


class TestSimulatePaths(unittest.TestCase):
    def test_shape_is_n_paths_by_n_steps_plus_one(self):
        paths = simulate_paths(100.0, 0.05, 0.2, 0.5, n_paths=50, n_steps=20, seed=1)
        self.assertEqual(paths.shape, (50, 21))

    def test_starts_at_spot(self):
        paths = simulate_paths(123.4, 0.05, 0.2, 0.5, n_paths=10, n_steps=5, seed=2)
        np.testing.assert_allclose(paths[:, 0], 123.4)

    def test_determinism(self):
        a = simulate_paths(100.0, 0.05, 0.2, 1.0, 10, 10, seed=7)
        b = simulate_paths(100.0, 0.05, 0.2, 1.0, 10, 10, seed=7)
        np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    unittest.main()
