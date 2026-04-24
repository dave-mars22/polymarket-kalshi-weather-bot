"""Tests for backend.core.vol_estimator.

Strategy: generate synthetic GBM price series with known (mu, sigma) under a
fixed seed, run estimators, assert recovered values land within theoretical
sampling error.
"""
from __future__ import annotations

import math
import unittest

import numpy as np

from backend.core.vol_estimator import (
    VolDriftEstimate,
    drift_annual,
    estimate,
    log_returns,
    realized_vol_equal,
    realized_vol_ewma,
)


def _synth_gbm_prices(
    S0: float, mu: float, sigma: float, periods_per_year: float, n_steps: int, seed: int
) -> np.ndarray:
    """Generate a synthetic GBM price series."""
    rng = np.random.default_rng(seed)
    dt = 1.0 / periods_per_year
    z = rng.standard_normal(n_steps)
    log_returns_sample = (mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z
    log_prices = np.log(S0) + np.cumsum(log_returns_sample)
    return np.concatenate([[S0], np.exp(log_prices)])


class TestLogReturns(unittest.TestCase):
    def test_basic(self):
        prices = [100.0, 110.0, 99.0]
        r = log_returns(prices)
        self.assertEqual(r.shape, (2,))
        np.testing.assert_allclose(r[0], math.log(110 / 100))
        np.testing.assert_allclose(r[1], math.log(99 / 110))

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            log_returns([100.0])

    def test_non_positive_prices_raise(self):
        with self.assertRaises(ValueError):
            log_returns([100.0, 0.0, 50.0])
        with self.assertRaises(ValueError):
            log_returns([100.0, -50.0])


class TestVolEstimatorRecovery(unittest.TestCase):
    """Given a synthetic GBM with known sigma, recover it within SE."""

    def _assert_recovers_sigma(self, true_sigma, n, periods, seed, rel_tol=0.15):
        prices = _synth_gbm_prices(100.0, 0.05, true_sigma, periods, n, seed)
        rets = log_returns(prices)
        est_eq = realized_vol_equal(rets, periods_per_year=periods)
        # SE of sample std for a normal sample of size n is sigma / sqrt(2n).
        # With rel_tol=0.15 we allow ~15% error, well above 4-sigma for n=1000
        # (4*SE ~ 9% relative).
        self.assertLess(
            abs(est_eq - true_sigma) / true_sigma, rel_tol,
            f"equal-weighted: estimated {est_eq:.4f} vs true {true_sigma:.4f}",
        )

    def test_recovers_low_vol_daily_equity(self):
        self._assert_recovers_sigma(true_sigma=0.15, n=1000, periods=252, seed=1)

    def test_recovers_moderate_vol(self):
        self._assert_recovers_sigma(true_sigma=0.30, n=1000, periods=365, seed=2)

    def test_recovers_high_vol_crypto(self):
        self._assert_recovers_sigma(true_sigma=0.80, n=1000, periods=365, seed=3)


class TestEWMA(unittest.TestCase):
    def test_ewma_matches_equal_weighted_on_stationary_vol(self):
        """For a long stationary series, EWMA and equal-weighted converge."""
        prices = _synth_gbm_prices(100.0, 0.0, 0.25, 365.0, n_steps=5000, seed=42)
        rets = log_returns(prices)
        eq = realized_vol_equal(rets, periods_per_year=365.0)
        ewma = realized_vol_ewma(rets, lambda_=0.94, periods_per_year=365.0)
        self.assertLess(abs(ewma - eq) / eq, 0.20, f"eq={eq:.4f}, ewma={ewma:.4f}")

    def test_ewma_reacts_faster_than_equal_weighted_to_vol_shift(self):
        """Series: 500 days at sigma=0.15, then 50 days at sigma=0.80.
        EWMA should report higher final vol than equal-weighted."""
        rng = np.random.default_rng(0)
        dt_365 = 1.0 / 365.0
        low = (0.0 - 0.5 * 0.15**2) * dt_365 + 0.15 * math.sqrt(dt_365) * rng.standard_normal(500)
        high = (0.0 - 0.5 * 0.80**2) * dt_365 + 0.80 * math.sqrt(dt_365) * rng.standard_normal(50)
        rets = np.concatenate([low, high])

        eq = realized_vol_equal(rets, periods_per_year=365.0)
        ewma = realized_vol_ewma(rets, lambda_=0.94, periods_per_year=365.0)
        self.assertGreater(ewma, eq, f"eq={eq:.4f}, ewma={ewma:.4f}")

    def test_lambda_out_of_range_raises(self):
        rets = np.array([0.01, -0.02, 0.005])
        with self.assertRaises(ValueError):
            realized_vol_ewma(rets, lambda_=0.0)
        with self.assertRaises(ValueError):
            realized_vol_ewma(rets, lambda_=1.0)
        with self.assertRaises(ValueError):
            realized_vol_ewma(rets, lambda_=-0.5)


class TestDrift(unittest.TestCase):
    def test_drift_recovery(self):
        """Given mu=0.10 and enough samples, recover within ~4*SE."""
        true_mu, true_sigma, periods, n = 0.10, 0.20, 252, 5000
        prices = _synth_gbm_prices(100.0, true_mu, true_sigma, periods, n, seed=5)
        rets = log_returns(prices)
        est = drift_annual(rets, periods_per_year=periods)
        # SE of annualized mean is sigma * sqrt(periods / n).
        se = true_sigma * math.sqrt(periods / n)
        self.assertLess(abs(est - true_mu), 4 * se,
                        f"est={est:.4f} vs true={true_mu:.4f}, 4*se={4*se:.4f}")


class TestEstimateEntryPoint(unittest.TestCase):
    def test_returns_dataclass(self):
        prices = _synth_gbm_prices(100.0, 0.05, 0.25, 365.0, 500, seed=1)
        est = estimate(prices, periods_per_year=365.0)
        self.assertIsInstance(est, VolDriftEstimate)
        self.assertEqual(est.periods_per_year, 365.0)
        self.assertEqual(est.n_returns, 500)

    def test_windowing(self):
        prices = _synth_gbm_prices(100.0, 0.05, 0.25, 365.0, 500, seed=1)
        est_windowed = estimate(
            prices, periods_per_year=365.0,
            vol_window=30, drift_window=90,
        )
        self.assertGreater(est_windowed.sigma_annual, 0)

    def test_periods_per_year_annualization(self):
        """Same daily returns, different periods_per_year => sigma scales as
        sqrt(periods)."""
        prices = _synth_gbm_prices(100.0, 0.0, 0.30, 365.0, 1000, seed=11)
        a = estimate(prices, periods_per_year=365.0, use_ewma=False)
        b = estimate(prices, periods_per_year=252.0, use_ewma=False)
        self.assertAlmostEqual(
            b.sigma_annual / a.sigma_annual, math.sqrt(252 / 365), places=6,
        )


class TestInputValidation(unittest.TestCase):
    def test_equal_vol_too_few_returns(self):
        with self.assertRaises(ValueError):
            realized_vol_equal(np.array([0.01]), periods_per_year=365.0)

    def test_ewma_empty_returns_raises(self):
        with self.assertRaises(ValueError):
            realized_vol_ewma(np.array([]), lambda_=0.94, periods_per_year=365.0)

    def test_drift_too_few_returns(self):
        with self.assertRaises(ValueError):
            drift_annual(np.array([0.01]), periods_per_year=365.0)


if __name__ == "__main__":
    unittest.main()
