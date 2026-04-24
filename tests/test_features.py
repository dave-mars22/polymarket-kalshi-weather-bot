"""Tests for slice 4: structured feature persistence on Signal + Trade rows.

Four scenarios exercised:
  1. BTC brain's TradingSignal.features survives to Signal.features
  2. MC brain's MonteCarloSignal.features survives to Signal.features
  3. Trade row inherits signal features at execution (tested by constructing
     a Trade directly — full scheduler loop is exercised in live run)
  4. Settlement merges outcome keys without dropping signal/exec keys
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.signals import generate_crypto_tech_signal, _persist_signals
from backend.core.settlement import _merge_settlement_features
from backend.data.crypto import CryptoMicrostructure
from backend.data.crypto_markets import CryptoUpDownMarket
from backend.models import database as db_mod
from backend.models.database import Signal, Trade


def _make_market(
    volume_24h: float = 200.0,
    underlying: str = "BTC",
    slug_tail: str = "1700000000",
) -> CryptoUpDownMarket:
    now = datetime.now(timezone.utc)
    # Window ends 4 minutes out so it's inside MIN_TIME_REMAINING (60s)
    # and MAX_TIME_REMAINING (1800s) — lets the time-filter pass.
    return CryptoUpDownMarket(
        slug=f"{underlying.lower()}-updown-5m-{slug_tail}",
        market_id=f"{underlying.lower()}-mkt-{slug_tail}",
        up_price=0.50,
        down_price=0.50,
        window_start=now,
        window_end=now + timedelta(minutes=4),
        volume=0.0,
        volume_24h=volume_24h,
        closed=False,
    )


def _mock_micro(price: float = 78_000.0) -> CryptoMicrostructure:
    return CryptoMicrostructure(
        rsi=25.0,
        momentum_1m=0.5,
        momentum_5m=0.3,
        momentum_15m=0.2,
        vwap=price,
        vwap_deviation=0.3,
        sma_crossover=0.15,
        volatility=0.05,
        price=price,
        source="coinbase_test",
    )


class _InMemoryDBMixin:
    """Swap SessionLocal for an in-memory SQLite engine for the test.

    `signals.py` and `mc_signals.py` import SessionLocal by name at module
    load time, so we patch those module bindings too — not just db_mod."""

    def setUp(self):
        import backend.core.signals as signals_mod
        import backend.core.mc_signals as mc_signals_mod

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        db_mod.Base.metadata.create_all(bind=engine)
        SL = sessionmaker(
            autocommit=False, autoflush=False, bind=engine,
        )
        self._orig_bindings = [
            (db_mod, db_mod.SessionLocal),
            (signals_mod, signals_mod.SessionLocal),
            (mc_signals_mod, mc_signals_mod.SessionLocal),
        ]
        db_mod.SessionLocal = SL
        signals_mod.SessionLocal = SL
        mc_signals_mod.SessionLocal = SL

    def tearDown(self):
        for mod, orig in self._orig_bindings:
            mod.SessionLocal = orig


class TestBTCSignalFeaturesPersisted(_InMemoryDBMixin, unittest.TestCase):
    def test_btc_features_round_trip_to_db(self):
        # Run the async generator on a dedicated loop so we don't close the
        # thread's default loop (which would break downstream tests that
        # rely on asyncio.get_event_loop()).
        async def _inner():
            market = _make_market(volume_24h=200.0, underlying="BTC")
            with patch(
                "backend.core.signals.compute_crypto_microstructure",
                return_value=_mock_micro(),
            ):
                return await generate_crypto_tech_signal(market, "BTC")

        loop = asyncio.new_event_loop()
        try:
            signal = loop.run_until_complete(_inner())
        finally:
            loop.close()
            asyncio.set_event_loop(asyncio.new_event_loop())

        self.assertIsNotNone(signal)
        self.assertIsInstance(signal.features, dict)
        for key in (
            "strategy", "underlying", "rsi", "momentum_1m", "momentum_5m",
            "momentum_15m", "vwap_deviation", "sma_crossover", "volatility",
            "composite_score", "model_prob_raw", "model_prob_clipped",
            "underlying_price", "market_volume_24h", "up_price", "down_price",
            "spread", "minutes_to_close", "convergence_score", "filter_status",
        ):
            self.assertIn(key, signal.features, f"missing key {key}")
        self.assertEqual(signal.features["strategy"], "crypto_tech_5m")
        self.assertEqual(signal.features["underlying"], "BTC")

        _persist_signals([signal])

        db = db_mod.SessionLocal()
        try:
            row = db.query(Signal).first()
            self.assertIsNotNone(row, "signal row should have been persisted")
            self.assertIsInstance(row.features, dict)
            self.assertEqual(row.features["strategy"], "crypto_tech_5m")
            self.assertEqual(row.features["underlying"], "BTC")
            self.assertAlmostEqual(row.features["rsi"], 25.0, places=2)
        finally:
            db.close()


class TestMCSignalFeaturesPersisted(_InMemoryDBMixin, unittest.TestCase):
    def test_mc_features_round_trip_to_db(self):
        # Build a MonteCarloSignal manually; avoid running the full scan.
        from backend.core.mc_signals import MonteCarloSignal, persist_mc_signals
        from backend.data.mc_markets import MonteCarloMarket

        mkt = MonteCarloMarket(
            ticker="KXBTCD-X-T90000",
            event_ticker="KXBTCD-X",
            venue="kalshi",
            underlying_asset="BTC",
            asset_class="crypto",
            direction="above",
            threshold=90_000.0,
            close_time=datetime.now(timezone.utc),
            yes_ask=0.20, yes_bid=0.19, no_ask=0.80, no_bid=0.79,
            raw_market={"rules_primary": "BTC above $90k at close"},
        )
        sig = MonteCarloSignal(
            market=mkt,
            direction="YES",
            model_probability=0.40,
            market_probability=0.20,
            raw_edge=0.20,
            net_edge=0.15,
            fee_cost=2.0,
            passes_threshold=True,
            suggested_size=5.0,
            reasoning="test",
            spot_used=80_000.0,
            vol_used=0.60,
            drift_used=0.0,
            years_to_expiry=0.01,
            features={
                "strategy": "monte_carlo",
                "contract_style": "european",
                "underlying": "BTC",
                "asset_class": "crypto",
                "spot_used": 80_000.0,
                "vol_used": 0.60,
                "drift_used": 0.0,
                "years_to_expiry": 0.01,
                "n_paths": 10_000,
                "model_probability": 0.40,
                "market_probability": 0.20,
                "raw_edge": 0.20,
                "net_edge": 0.15,
                "fee_cost": 2.0,
                "ticker": "KXBTCD-X-T90000",
                "threshold": 90_000.0,
                "threshold_upper": None,
                "yes_ask": 0.20, "yes_bid": 0.19,
                "no_ask": 0.80, "no_bid": 0.79,
                "direction_chosen": "YES",
            },
        )

        written = persist_mc_signals([sig])
        self.assertEqual(written, 1)

        db = db_mod.SessionLocal()
        try:
            row = db.query(Signal).filter(Signal.market_type == "monte_carlo").first()
            self.assertIsNotNone(row)
            self.assertIsInstance(row.features, dict)
            self.assertEqual(row.features["strategy"], "monte_carlo")
            self.assertEqual(row.features["contract_style"], "european")
            self.assertEqual(row.features["direction_chosen"], "YES")
            self.assertEqual(row.features["n_paths"], 10_000)
        finally:
            db.close()


class TestTradeFeaturesInheritFromSignal(_InMemoryDBMixin, unittest.TestCase):
    """Construct a Trade directly with the scheduler's merge pattern
    (signal features + exec context) and verify round-trip."""

    def test_trade_features_merge_signal_and_exec(self):
        signal_features = {
            "strategy": "crypto_tech_5m",
            "underlying": "ETH",
            "rsi": 28.5,
            "composite_score": 0.21,
        }
        exec_ctx = {
            "exec_hour_utc": 14,
            "exec_minute": 37,
            "exec_day_of_week": 2,
            "exec_total_pending": 5,
            "exec_pending_this_underlying": 1,
            "exec_bankroll_at_entry": 10_000.0,
        }
        merged = {**exec_ctx, **signal_features}

        db = db_mod.SessionLocal()
        try:
            t = Trade(
                market_ticker="eth-mkt-123",
                platform="polymarket",
                market_type="btc",
                underlying_asset="ETH",
                asset_class="crypto",
                direction="up",
                entry_price=0.5,
                size=10.0,
                model_probability=0.55,
                market_price_at_entry=0.5,
                edge_at_entry=0.05,
                features=merged,
            )
            db.add(t)
            db.commit()

            row = db.query(Trade).first()
            self.assertIsInstance(row.features, dict)
            # Signal keys preserved
            self.assertEqual(row.features["strategy"], "crypto_tech_5m")
            self.assertEqual(row.features["underlying"], "ETH")
            self.assertEqual(row.features["rsi"], 28.5)
            # Exec keys present
            self.assertEqual(row.features["exec_hour_utc"], 14)
            self.assertEqual(row.features["exec_total_pending"], 5)
            self.assertEqual(row.features["exec_bankroll_at_entry"], 10_000.0)
        finally:
            db.close()


class TestSettlementFeaturesMerged(_InMemoryDBMixin, unittest.TestCase):
    """Settlement must layer outcome keys onto features without clobbering
    existing signal+exec keys."""

    def test_merge_preserves_signal_and_exec_keys(self):
        entry_features = {
            "strategy": "crypto_tech_5m",
            "underlying": "BTC",
            "rsi": 25.0,
            "exec_hour_utc": 9,
            "exec_bankroll_at_entry": 10_000.0,
        }
        entry_ts = datetime.utcnow()

        db = db_mod.SessionLocal()
        try:
            t = Trade(
                market_ticker="btc-mkt-xyz",
                platform="polymarket",
                market_type="btc",
                underlying_asset="BTC",
                asset_class="crypto",
                direction="up",
                entry_price=0.45,
                size=20.0,
                timestamp=entry_ts,
                model_probability=0.60,
                market_price_at_entry=0.45,
                edge_at_entry=0.15,
                features=dict(entry_features),
                settled=False,
            )
            db.add(t)
            db.commit()

            # Simulate settlement: UP won, model was right.
            t.pnl = 20.0 * (1.0 - 0.45)  # win on YES/UP
            _merge_settlement_features(t, settlement_value=1.0)
            db.commit()

            row = db.query(Trade).first()
            feats = row.features
            self.assertIsInstance(feats, dict)
            # Entry keys intact
            self.assertEqual(feats["strategy"], "crypto_tech_5m")
            self.assertEqual(feats["underlying"], "BTC")
            self.assertEqual(feats["rsi"], 25.0)
            self.assertEqual(feats["exec_hour_utc"], 9)
            # Outcome keys added
            self.assertEqual(feats["settled_outcome"], 1.0)
            self.assertAlmostEqual(feats["realized_pnl"], 11.0, places=2)
            self.assertIn("settlement_timestamp", feats)
            self.assertIn("minutes_to_settlement", feats)
            # predicted UP prob = 0.60; outcome = 1.0; diff = -0.40
            self.assertAlmostEqual(feats["predicted_vs_realized"], -0.40, places=4)
        finally:
            db.close()

    def test_merge_preserves_on_collision(self):
        """Defensive: if entry features ever collide with an outcome key,
        the entry value wins (signal data is authoritative)."""
        t = Trade(
            market_ticker="x",
            platform="polymarket",
            market_type="btc",
            underlying_asset="BTC",
            asset_class="crypto",
            direction="up",
            entry_price=0.5,
            size=1.0,
            timestamp=datetime.utcnow(),
            model_probability=0.5,
            market_price_at_entry=0.5,
            edge_at_entry=0.0,
            features={"settled_outcome": "entry-claims-this"},
            settled=False,
        )
        t.pnl = 0.5
        _merge_settlement_features(t, settlement_value=0.0)
        self.assertEqual(t.features["settled_outcome"], "entry-claims-this")


if __name__ == "__main__":
    unittest.main()
