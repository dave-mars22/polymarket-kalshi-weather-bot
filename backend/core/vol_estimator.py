"""Volatility and drift estimation from a historical price series.

All public functions return *annualized* values so they feed directly into
monte_carlo.simulate_terminal_prices. Caller chooses periods_per_year to
match the sampling frequency:
    - 365 for daily crypto (trades 24/7)
    - 252 for daily US equities (trading days per year)
    - 52 / 12 for weekly / monthly bars

EWMA is the default: down-weights stale observations with RiskMetrics'
lambda=0.94, ~5-period half-life (~25 trading days). Equal-weighted is kept
as a sanity check / fallback.

Drift note: short-window drift estimates are very noisy. SE of the sample
mean ~ sigma / sqrt(n). For a 90-day window on 70% vol crypto, annualized
SE is ~117% — the estimate is indistinguishable from zero. For short-dated
contracts this matters little because drift scales linearly with T while
vol scales with sqrt(T). Callers may pass drift=0 as a robust default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class VolDriftEstimate:
    mu_annual: float          # annualized drift
    sigma_annual: float       # annualized volatility
    n_returns: int            # number of log-return observations used
    periods_per_year: float   # unit conversion factor used


def log_returns(prices: Sequence[float]) -> np.ndarray:
    """r_t = ln(P_t / P_{t-1}) for t >= 1."""
    arr = np.asarray(prices, dtype=float)
    if arr.ndim != 1:
        raise ValueError("prices must be 1-D")
    if arr.size < 2:
        raise ValueError(f"need at least 2 prices, got {arr.size}")
    if np.any(arr <= 0):
        raise ValueError("all prices must be > 0")
    return np.diff(np.log(arr))


def realized_vol_equal(
    log_rets: np.ndarray, periods_per_year: float
) -> float:
    """Equal-weighted sample std of log-returns, annualized.

    Uses ddof=1 (Bessel's correction) for unbiased variance estimation.
    """
    if log_rets.size < 2:
        raise ValueError(f"need at least 2 log-returns, got {log_rets.size}")
    return float(np.std(log_rets, ddof=1) * math.sqrt(periods_per_year))


def realized_vol_ewma(
    log_rets: np.ndarray,
    lambda_: float = 0.94,
    periods_per_year: float = 365.0,
) -> float:
    """RiskMetrics EWMA volatility, annualized.

    Recursive formula:
        variance_t = lambda * variance_{t-1} + (1 - lambda) * r_t^2
    initialized with variance_0 = r_0^2. After ~50 observations the initial
    condition contributes < 5% (lambda=0.94: 0.94^50 ~= 0.046).
    """
    if not 0.0 < lambda_ < 1.0:
        raise ValueError(f"lambda_ must be in (0, 1), got {lambda_}")
    if log_rets.size < 1:
        raise ValueError("need at least 1 log-return")

    var = log_rets[0] ** 2
    for r in log_rets[1:]:
        var = lambda_ * var + (1.0 - lambda_) * r * r

    return float(math.sqrt(var) * math.sqrt(periods_per_year))


def drift_annual(log_rets: np.ndarray, periods_per_year: float) -> float:
    """Annualized drift mu estimated from sample mean of log-returns.

    The sample mean of log-returns estimates (mu - sigma^2/2) * dt, where
    dt = 1/periods_per_year. We add back sigma^2/2 so the returned value
    is an estimate of mu itself, which is what GBM simulation expects.
    """
    if log_rets.size < 2:
        raise ValueError(f"need at least 2 log-returns, got {log_rets.size}")
    mean_r = float(np.mean(log_rets))
    var_r = float(np.var(log_rets, ddof=1))
    return mean_r * periods_per_year + 0.5 * var_r * periods_per_year


def estimate(
    prices: Sequence[float],
    periods_per_year: float,
    use_ewma: bool = True,
    ewma_lambda: float = 0.94,
    vol_window: Optional[int] = None,
    drift_window: Optional[int] = None,
) -> VolDriftEstimate:
    """Estimate (mu_annual, sigma_annual) from a price series.

    Args:
        prices: chronological price series (oldest first).
        periods_per_year: 365 for daily crypto, 252 for daily US equities, etc.
        use_ewma: EWMA vol if True, else equal-weighted.
        ewma_lambda: RiskMetrics smoothing factor (0 < lambda < 1).
        vol_window: use only the last N log-returns for vol. None = all.
        drift_window: use only the last N log-returns for drift. None = all.
    """
    all_returns = log_returns(prices)

    vol_rets = all_returns if vol_window is None else all_returns[-vol_window:]
    drift_rets = all_returns if drift_window is None else all_returns[-drift_window:]

    if use_ewma:
        sigma = realized_vol_ewma(vol_rets, ewma_lambda, periods_per_year)
    else:
        sigma = realized_vol_equal(vol_rets, periods_per_year)

    mu = drift_annual(drift_rets, periods_per_year)

    return VolDriftEstimate(
        mu_annual=mu,
        sigma_annual=sigma,
        n_returns=len(all_returns),
        periods_per_year=periods_per_year,
    )
