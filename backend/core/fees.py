"""
Fee models for Kalshi and Polymarket.

Every edge calculation should pass through net_edge() rather than computing
model_prob - market_prob directly. Raw edge is kept for dashboards/debugging,
but trade decisions use net_edge.

Sources:
- Kalshi fee schedule: https://kalshi.com/docs/fees
  Formula: fee = ceil(0.07 * contracts * price * (1 - price) * 100) / 100
  This is asymmetric and peaks at price = 0.50.
- Polymarket: 0% maker/taker on most markets as of 2026. We model slippage
  as a flat bps cost to account for thin depth on some markets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Venue = Literal["kalshi", "polymarket"]
MarketType = Literal["btc", "monte_carlo"]


@dataclass
class FeeBreakdown:
    """Dollar costs of a round trip (entry + exit/settlement)."""
    exchange_fee: float
    slippage: float
    gas: float = 0.0

    @property
    def total(self) -> float:
        return self.exchange_fee + self.slippage + self.gas


class FeeModel:
    """Base class. Subclass per venue."""

    def estimate_entry_cost(
        self, entry_price: float, size_usd: float
    ) -> FeeBreakdown:
        raise NotImplementedError

    def estimate_round_trip_cost(
        self, entry_price: float, size_usd: float
    ) -> FeeBreakdown:
        """
        Round trip = entry + settlement. For binary contracts held to expiry,
        there's no exit fee (contract pays $1 or $0), but we still double
        slippage to account for the implicit cost of entering at ask.

        If you later add early exits, override this.
        """
        entry = self.estimate_entry_cost(entry_price, size_usd)
        # Settlement is free on both venues; only entry has exchange fees.
        # But effective entry slippage is captured once, not doubled,
        # because we're already pricing at the ask.
        return entry


class KalshiFeeModel(FeeModel):
    """
    Kalshi charges a trading fee on both sides of a trade.
    Formula per contract: ceil(0.07 * price * (1 - price) * 100) / 100

    Examples:
      price=0.50 -> 0.07 * 0.25 = 0.0175 -> ceil(1.75)/100 = $0.02/contract
      price=0.80 -> 0.07 * 0.16 = 0.0112 -> ceil(1.12)/100 = $0.02/contract
      price=0.95 -> 0.07 * 0.0475 = 0.00332 -> ceil(0.332)/100 = $0.01/contract
    """

    def __init__(self, slippage_bps: int = 50):
        """slippage_bps: basis points of notional lost to slippage."""
        self.slippage_bps = slippage_bps

    def _per_contract_fee(self, price: float) -> float:
        # Clamp to avoid edge cases at 0 or 1
        p = max(0.01, min(0.99, price))
        raw = 0.07 * p * (1 - p)
        # Ceiling to nearest cent
        return math.ceil(raw * 100) / 100

    def estimate_entry_cost(
        self, entry_price: float, size_usd: float
    ) -> FeeBreakdown:
        contracts = size_usd / entry_price if entry_price > 0 else 0
        exchange_fee = self._per_contract_fee(entry_price) * contracts
        slippage = size_usd * (self.slippage_bps / 10_000)
        return FeeBreakdown(
            exchange_fee=exchange_fee, slippage=slippage, gas=0.0
        )


class PolymarketFeeModel(FeeModel):
    """
    Polymarket currently charges 0% maker/taker on most markets. We still
    model slippage and a small gas allowance for on-chain settlement.
    """

    def __init__(
        self,
        taker_fee_pct: float = 0.0,
        slippage_bps: int = 50,
        gas_usd: float = 0.10,
    ):
        self.taker_fee_pct = taker_fee_pct
        self.slippage_bps = slippage_bps
        self.gas_usd = gas_usd

    def estimate_entry_cost(
        self, entry_price: float, size_usd: float
    ) -> FeeBreakdown:
        exchange_fee = size_usd * self.taker_fee_pct
        slippage = size_usd * (self.slippage_bps / 10_000)
        return FeeBreakdown(
            exchange_fee=exchange_fee, slippage=slippage, gas=self.gas_usd
        )


# --------------------------------------------------------------------------
# Factory + high-level edge helper
# --------------------------------------------------------------------------

def get_fee_model(
    venue: Venue,
    polymarket_slippage_bps: int = 10,
    kalshi_slippage_bps: int = 50,
    market_type: str = "btc",
) -> FeeModel:
    """
    Build the right fee model for (venue, market_type). Slippage defaults
    differ because Polymarket crypto books are tighter than Kalshi books.

    market_type is recognized explicitly:
      - "btc":         Polymarket crypto 5-min markets (tight book) -> polymarket_slippage_bps
      - "monte_carlo": Kalshi GBM-priced contracts                  -> kalshi_slippage_bps
    Any other value raises to prevent silent mis-routing.
    """
    if market_type == "btc":
        slippage = polymarket_slippage_bps
    elif market_type == "monte_carlo":
        slippage = kalshi_slippage_bps
    else:
        raise ValueError(
            f"Unknown market_type: {market_type!r} (expected 'btc' or 'monte_carlo')"
        )

    if venue == "kalshi":
        return KalshiFeeModel(slippage_bps=slippage)
    if venue == "polymarket":
        return PolymarketFeeModel(slippage_bps=slippage)
    raise ValueError(f"Unknown venue: {venue!r}")


def net_edge(
    model_prob: float,
    market_prob: float,
    entry_price: float,
    size_usd: float,
    venue: Venue,
    market_type: str = "btc",
    polymarket_slippage_bps: int = 10,
    kalshi_slippage_bps: int = 50,
) -> tuple[float, float, FeeBreakdown]:
    """
    Returns (raw_edge, net_edge, fee_breakdown).

    raw_edge = model_prob - market_prob  (what your bot currently computes)
    net_edge = raw_edge - (fees_as_fraction_of_notional)

    Both are expressed as probabilities (e.g., 0.08 = 8%).

    Trade gate should compare net_edge to the threshold, not raw_edge.
    """
    raw = model_prob - market_prob

    fee_model = get_fee_model(
        venue,
        polymarket_slippage_bps=polymarket_slippage_bps,
        kalshi_slippage_bps=kalshi_slippage_bps,
        market_type=market_type,
    )
    costs = fee_model.estimate_round_trip_cost(entry_price, size_usd)

    # Convert dollar costs to edge-equivalent (fraction of notional).
    # If a trade risks $size_usd and costs $X in fees, that's X/size_usd of edge lost.
    fee_as_edge = costs.total / size_usd if size_usd > 0 else 0.0

    # Fees always reduce the UP-side post-fee edge. Sign-preserving: if
    # the caller's view is bearish (raw < 0), net is more negative.
    # Callers who pick a direction separately (e.g. BTC signals.py) should
    # compute their direction-specific post-fee edge from (raw_edge_val,
    # fee_breakdown.total / size_usd) rather than reinterpret this value's
    # sign. A prior version had a sign-flip branch for raw<=0; it produced
    # wrong magnitudes (with the wrong sign) when fees exceeded |raw|.
    net = raw - fee_as_edge

    return raw, net, costs
    