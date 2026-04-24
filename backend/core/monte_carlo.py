"""Geometric Brownian Motion simulator for binary-contract pricing.

Under GBM:
    S_T = S_0 * exp((mu - sigma^2 / 2) * T + sigma * sqrt(T) * Z),   Z ~ N(0, 1)

Convention used throughout this module: drift (mu) and volatility (sigma) are
*annualized*, and T (years_to_expiry) is expressed in years. The vol_estimator
module is responsible for producing values in these units.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class SimulationResult:
    """Outcome of one Monte Carlo run.

    Keeps the terminal-price array so downstream probability queries don't
    re-run the simulation. For a single contract we typically call one of
    prob_above / prob_below / prob_in_range once, but the caller may also
    want multiple thresholds against the same paths.
    """
    spot: float
    drift: float
    vol: float
    years_to_expiry: float
    n_paths: int
    terminal_prices: np.ndarray  # shape (n_paths,)

    def prob_above(self, threshold: float) -> float:
        if self.n_paths == 0:
            return 0.5
        return float((self.terminal_prices > threshold).mean())

    def prob_below(self, threshold: float) -> float:
        return 1.0 - self.prob_above(threshold)

    def prob_in_range(self, low: float, high: float) -> float:
        """P(low < S_T <= high). Low-exclusive / high-inclusive.

        Boundary convention chosen so prob_below(L) + prob_in_range(L, K) +
        prob_above(K) exactly partitions probability with no double-counting
        (paths landing exactly on a boundary have measure zero under GBM).
        """
        if low > high:
            raise ValueError(f"low ({low}) must be <= high ({high})")
        arr = self.terminal_prices
        return float(((arr > low) & (arr <= high)).mean())


def simulate_terminal_prices(
    spot: float,
    drift_annual: float,
    vol_annual: float,
    years_to_expiry: float,
    n_paths: int = 10_000,
    seed: Optional[int] = None,
) -> SimulationResult:
    """Sample `n_paths` terminal prices under GBM.

    Args:
        spot: S_0, current price. Must be > 0.
        drift_annual: mu, annualized expected log-return drift.
        vol_annual: sigma, annualized log-return volatility. Must be >= 0.
        years_to_expiry: T in years. Must be > 0.
        n_paths: number of Monte Carlo paths.
        seed: optional RNG seed for reproducibility (test determinism).
    """
    if spot <= 0:
        raise ValueError(f"spot must be > 0, got {spot}")
    if vol_annual < 0:
        raise ValueError(f"vol_annual must be >= 0, got {vol_annual}")
    if years_to_expiry <= 0:
        raise ValueError(f"years_to_expiry must be > 0, got {years_to_expiry}")
    if n_paths < 1:
        raise ValueError(f"n_paths must be >= 1, got {n_paths}")

    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n_paths)

    drift_term = (drift_annual - 0.5 * vol_annual ** 2) * years_to_expiry
    diffusion_term = vol_annual * math.sqrt(years_to_expiry) * z

    terminal = spot * np.exp(drift_term + diffusion_term)

    return SimulationResult(
        spot=spot,
        drift=drift_annual,
        vol=vol_annual,
        years_to_expiry=years_to_expiry,
        n_paths=n_paths,
        terminal_prices=terminal,
    )


def simulate_paths(
    spot: float,
    drift_annual: float,
    vol_annual: float,
    years_to_expiry: float,
    n_paths: int,
    n_steps: int,
    seed: Optional[int] = None,
) -> np.ndarray:
    """Simulate n_paths full GBM price paths with n_steps time-steps.

    Returns an array of shape (n_paths, n_steps + 1) where column 0 is S_0
    and column n_steps is S_T. Intended for barrier-contract verification
    (MC estimate of one-touch probability). Slow relative to
    simulate_terminal_prices; don't use for production pricing.
    """
    if spot <= 0:
        raise ValueError(f"spot must be > 0, got {spot}")
    if vol_annual < 0:
        raise ValueError(f"vol_annual must be >= 0, got {vol_annual}")
    if years_to_expiry <= 0:
        raise ValueError(f"years_to_expiry must be > 0, got {years_to_expiry}")
    if n_paths < 1 or n_steps < 1:
        raise ValueError(f"n_paths, n_steps must be >= 1")

    rng = np.random.default_rng(seed)
    dt = years_to_expiry / n_steps
    drift_term = (drift_annual - 0.5 * vol_annual ** 2) * dt
    diffusion_coef = vol_annual * math.sqrt(dt)

    increments = drift_term + diffusion_coef * rng.standard_normal((n_paths, n_steps))
    log_paths = np.cumsum(increments, axis=1)
    # Prepend time-0 so column 0 is S_0.
    log_paths = np.concatenate(
        [np.zeros((n_paths, 1), dtype=log_paths.dtype), log_paths],
        axis=1,
    )
    return spot * np.exp(log_paths)


def prob_one_touch_above_analytic(
    spot: float,
    barrier: float,
    drift_annual: float,
    vol_annual: float,
    years_to_expiry: float,
) -> float:
    """Closed-form P(max_{t in [0,T]} S_t >= B) under GBM with drift.

    Reflection-principle formula (Shreve Vol II, Theorem 7.2.1):
        mu_tilde = mu - sigma^2 / 2
        d1 = (ln(S_0/B) + mu_tilde * T) / (sigma * sqrt(T))
        d2 = (ln(S_0/B) - mu_tilde * T) / (sigma * sqrt(T))
        P = Phi(d1) + (B/S_0)^(2 * mu_tilde / sigma^2) * Phi(d2)

    If B <= S_0 the barrier is already touched at t=0 and P = 1.
    """
    if spot <= 0 or barrier <= 0:
        raise ValueError("spot and barrier must both be > 0")
    if vol_annual <= 0 or years_to_expiry <= 0:
        raise ValueError("vol_annual and years_to_expiry must both be > 0")

    if barrier <= spot:
        return 1.0

    mu_tilde = drift_annual - 0.5 * vol_annual ** 2
    log_ratio = math.log(spot / barrier)        # < 0 since barrier > spot
    sigma_root_t = vol_annual * math.sqrt(years_to_expiry)
    d1 = (log_ratio + mu_tilde * years_to_expiry) / sigma_root_t
    d2 = (log_ratio - mu_tilde * years_to_expiry) / sigma_root_t
    power = 2.0 * mu_tilde / (vol_annual ** 2)
    bs_power = (barrier / spot) ** power
    return _phi(d1) + bs_power * _phi(d2)


def prob_one_touch_below_analytic(
    spot: float,
    barrier: float,
    drift_annual: float,
    vol_annual: float,
    years_to_expiry: float,
) -> float:
    """Closed-form P(min_{t in [0,T]} S_t <= B) under GBM with drift.

    Mirror of prob_one_touch_above_analytic via the reflection of BM
    around 0. The barrier-power prefactor is the same form; the d1/d2
    drift signs flip:
        d1 = (ln(B/S_0) - mu_tilde * T) / (sigma * sqrt(T))
        d2 = (ln(B/S_0) + mu_tilde * T) / (sigma * sqrt(T))
        P = Phi(d1) + (B/S_0)^(2 * mu_tilde / sigma^2) * Phi(d2)

    If B >= S_0 the barrier is already touched at t=0 and P = 1.
    """
    if spot <= 0 or barrier <= 0:
        raise ValueError("spot and barrier must both be > 0")
    if vol_annual <= 0 or years_to_expiry <= 0:
        raise ValueError("vol_annual and years_to_expiry must both be > 0")

    if barrier >= spot:
        return 1.0

    mu_tilde = drift_annual - 0.5 * vol_annual ** 2
    log_ratio = math.log(barrier / spot)        # < 0 since barrier < spot
    sigma_root_t = vol_annual * math.sqrt(years_to_expiry)
    d1 = (log_ratio - mu_tilde * years_to_expiry) / sigma_root_t
    d2 = (log_ratio + mu_tilde * years_to_expiry) / sigma_root_t
    power = 2.0 * mu_tilde / (vol_annual ** 2)
    bs_power = (barrier / spot) ** power
    return _phi(d1) + bs_power * _phi(d2)


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above_analytic(
    spot: float,
    threshold: float,
    drift_annual: float,
    vol_annual: float,
    years_to_expiry: float,
) -> float:
    """Closed-form P(S_T > K) under GBM (lognormal CDF).

    Derivation: log(S_T / S_0) ~ Normal(mu_adj, vol_adj^2) where
        mu_adj  = (mu - sigma^2 / 2) * T
        vol_adj = sigma * sqrt(T)
    Then P(S_T > K) = Phi( (log(S_0 / K) + mu_adj) / vol_adj ).

    Matches the Black-Scholes d2 formula (probability of exercise under
    the P-measure here, since we pass real-world drift rather than r - q).
    Used as the oracle for Monte Carlo convergence tests.
    """
    if spot <= 0 or threshold <= 0:
        raise ValueError("spot and threshold must both be > 0")
    if vol_annual <= 0 or years_to_expiry <= 0:
        raise ValueError("vol_annual and years_to_expiry must both be > 0")
    d = (
        math.log(spot / threshold)
        + (drift_annual - 0.5 * vol_annual ** 2) * years_to_expiry
    ) / (vol_annual * math.sqrt(years_to_expiry))
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2.0)))
