"""Slice D2: additive backend API expansion.

Verifies:
  - SignalResponse carries underlying_asset / asset_class / contract_style
  - TradeResponse carries the same attribution fields
  - /api/dashboard surfaces per_strategy_stats, per_asset_stats, mc_portfolio,
    and multi_microstructure (all additive; old fields untouched)
  - /api/mc/portfolio returns a valid McPortfolioStatus
  - /api/mc/signals and /api/mc/trades round-trip DB rows with market_type filter
  - All pre-existing DashboardData top-level keys remain present

Tests stub out the network-hitting calls that /api/dashboard makes so the
suite is hermetic. The contract we care about is response shape, not the
freshness of live market data.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models import database as db_mod
from backend.models.database import BotState, Signal, Trade


def _build_test_app_with_db():
    """Create a TestClient backed by an in-memory SQLite DB.

    StaticPool pins every connection to the same underlying :memory: DB,
    otherwise SQLite gives each connection a fresh empty schema and the
    endpoint's session sees `no such table: trades`.

    Returns (client, SessionLocal, reset_callable). The reset callable
    undoes the TestClient + SessionLocal patching when the test is done.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_mod.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Swap SessionLocal everywhere the api module or its helpers imported it.
    # `from backend.models.database import SessionLocal` in backend.api.main
    # captured the original binding at import time — patch both.
    import backend.api.main as api_main
    originals = {
        "db_mod.SessionLocal": db_mod.SessionLocal,
        "api_main.SessionLocal": api_main.SessionLocal,
    }
    db_mod.SessionLocal = TestSession
    api_main.SessionLocal = TestSession

    # Override the request-scoped get_db dependency so endpoints read from
    # the test DB rather than the module-global production session.
    def _override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    api_main.app.dependency_overrides[db_mod.get_db] = _override_get_db

    client = TestClient(api_main.app)

    def reset():
        api_main.app.dependency_overrides.pop(db_mod.get_db, None)
        db_mod.SessionLocal = originals["db_mod.SessionLocal"]
        api_main.SessionLocal = originals["api_main.SessionLocal"]

    return client, TestSession, reset


def _seed_bot_state(SessionLocal):
    db = SessionLocal()
    try:
        db.add(BotState(bankroll=10000.0, total_trades=0, winning_trades=0,
                        total_pnl=0.0, is_running=True))
        db.commit()
    finally:
        db.close()


def _seed_mc_signal_and_trade(SessionLocal):
    db = SessionLocal()
    try:
        db.add(Signal(
            market_ticker="KXBTCMAXMON-26APR30-8000000",
            platform="kalshi",
            market_type="monte_carlo",
            underlying_asset="BTC",
            asset_class="crypto",
            contract_style="one_touch_above",
            timestamp=datetime.utcnow(),
            direction="yes",
            model_probability=0.62,
            market_price=0.50,
            edge=0.12,
            confidence=0.0,
            kelly_fraction=0.01,
            suggested_size=10.0,
            sources=["gbm_one_touch_above"],
            reasoning="test",
            executed=False,
        ))
        db.add(Trade(
            market_ticker="KXBTCMAXMON-26APR30-8000000",
            platform="kalshi",
            event_slug="KXBTCMAXMON-26APR30",
            market_type="monte_carlo",
            underlying_asset="BTC",
            asset_class="crypto",
            contract_style="one_touch_above",
            direction="yes",
            entry_price=0.50,
            size=10.0,
            timestamp=datetime.utcnow(),
            settled=False,
            result="pending",
            model_probability=0.62,
            market_price_at_entry=0.50,
            edge_at_entry=0.12,
        ))
        db.commit()
    finally:
        db.close()


def _seed_crypto_tech_trade(SessionLocal, underlying: str = "BTC"):
    db = SessionLocal()
    try:
        db.add(Trade(
            market_ticker=f"{underlying.lower()}-5m-1700000000",
            platform="polymarket",
            event_slug=f"{underlying.lower()}-updown-5m-1700000000",
            market_type="btc",
            underlying_asset=underlying,
            asset_class="crypto",
            direction="up",
            entry_price=0.5,
            size=5.0,
            timestamp=datetime.utcnow(),
            settled=False,
            result="pending",
            model_probability=0.55,
            market_price_at_entry=0.50,
            edge_at_entry=0.05,
        ))
        db.commit()
    finally:
        db.close()


# Shared patches: the dashboard endpoint must not hit CoinGecko / Polymarket /
# Coinbase during tests. These two decorators stub every network-bound call
# touched by /api/dashboard (and its helpers) with AsyncMocks returning empty
# results. Tests that need per-case stubs override on top.
def _patch_network(test_fn):
    """Decorator stacking the set of async/sync network mocks we need."""
    # The order below matches the expected *args the test method will receive
    # from the mock.patch decorators. Patches are applied bottom-up.
    test_fn = patch(
        "backend.api.main.compute_crypto_microstructure",
        new=AsyncMock(return_value=None),
    )(test_fn)
    test_fn = patch(
        "backend.api.main.fetch_crypto_price",
        new=AsyncMock(return_value=None),
    )(test_fn)
    test_fn = patch(
        "backend.api.main.fetch_active_crypto_markets",
        new=AsyncMock(return_value=[]),
    )(test_fn)
    test_fn = patch(
        "backend.api.main.scan_for_signals",
        new=AsyncMock(return_value=[]),
    )(test_fn)
    return test_fn


class TestApiExpansion(unittest.TestCase):
    def setUp(self):
        self.client, self.SessionLocal, self._reset = _build_test_app_with_db()
        _seed_bot_state(self.SessionLocal)

    def tearDown(self):
        self._reset()

    # --- Signal / Trade attribution fields --------------------------------

    def test_trade_response_includes_underlying(self):
        _seed_mc_signal_and_trade(self.SessionLocal)
        r = self.client.get("/api/trades")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertTrue(rows, "expected at least one trade")
        t = rows[0]
        # Old fields still there
        for key in ("id", "market_ticker", "platform", "direction",
                    "entry_price", "size", "timestamp", "settled", "result", "pnl"):
            self.assertIn(key, t, f"old field {key!r} dropped from TradeResponse")
        # New attribution fields present
        self.assertEqual(t["underlying_asset"], "BTC")
        self.assertEqual(t["asset_class"], "crypto")
        self.assertEqual(t["contract_style"], "one_touch_above")
        self.assertEqual(t["market_type"], "monte_carlo")

    def test_mc_signals_response_includes_underlying(self):
        _seed_mc_signal_and_trade(self.SessionLocal)
        r = self.client.get("/api/mc/signals?limit=10")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertTrue(rows, "expected at least one MC signal")
        sig = rows[0]
        self.assertEqual(sig["underlying_asset"], "BTC")
        self.assertEqual(sig["asset_class"], "crypto")
        self.assertEqual(sig["contract_style"], "one_touch_above")

    # --- /api/mc/portfolio -----------------------------------------------

    def test_mc_portfolio_endpoint(self):
        _seed_mc_signal_and_trade(self.SessionLocal)
        r = self.client.get("/api/mc/portfolio")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("pilot_bankroll_target", "realized_pilot_bankroll",
                    "open_positions", "total_allocated",
                    "signals_last_24h", "actionable_signals_last_24h",
                    "next_scheduled_scan"):
            self.assertIn(key, body, f"missing key {key!r}")
        self.assertEqual(len(body["open_positions"]), 1)
        pos = body["open_positions"][0]
        self.assertEqual(pos["underlying"], "BTC")
        self.assertEqual(pos["market_ticker"], "KXBTCMAXMON-26APR30-8000000")
        # Settlement date parsed from ticker (26APR30 -> 2026-04-30)
        self.assertIsNotNone(pos["expected_settlement"])
        self.assertIn("2026-04-30", pos["expected_settlement"])
        # total_allocated = sum of open-position sizes
        self.assertAlmostEqual(body["total_allocated"], 10.0)
        # signals_last_24h counts the seeded signal
        self.assertGreaterEqual(body["signals_last_24h"], 1)

    # --- /api/dashboard: new additive fields ------------------------------

    @_patch_network
    def test_dashboard_has_per_strategy_stats(self, *_mocks):
        _seed_crypto_tech_trade(self.SessionLocal, "BTC")
        _seed_mc_signal_and_trade(self.SessionLocal)
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("per_strategy_stats", body)
        strategies = {row["strategy"] for row in body["per_strategy_stats"]}
        self.assertIn("crypto_tech", strategies)
        self.assertIn("monte_carlo", strategies)
        # Every row has the full schema
        for row in body["per_strategy_stats"]:
            for key in ("total_trades", "settled_trades", "pending_trades",
                        "wins", "losses", "win_rate", "total_pnl", "pnl_24h",
                        "allocated_bankroll", "realized_bankroll"):
                self.assertIn(key, row)

    @_patch_network
    def test_dashboard_has_per_asset_stats(self, *_mocks):
        _seed_crypto_tech_trade(self.SessionLocal, "BTC")
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("per_asset_stats", body)
        underlyings = {row["underlying"] for row in body["per_asset_stats"]}
        # All four tech underlyings present regardless of trade history
        for expected in ("BTC", "ETH", "SOL", "XRP"):
            self.assertIn(expected, underlyings,
                          f"{expected} missing from per_asset_stats")
        # BTC should now have the seeded pending trade
        btc_row = next(row for row in body["per_asset_stats"]
                       if row["underlying"] == "BTC")
        self.assertEqual(btc_row["pending_trades"], 1)
        self.assertEqual(btc_row["total_trades"], 1)

    @_patch_network
    def test_dashboard_has_mc_portfolio_and_multi_micro(self, *_mocks):
        _seed_mc_signal_and_trade(self.SessionLocal)
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("mc_portfolio", body)
        self.assertIsNotNone(body["mc_portfolio"])
        self.assertEqual(len(body["mc_portfolio"]["open_positions"]), 1)
        # multi_microstructure present even if empty (network mocked to None)
        self.assertIn("multi_microstructure", body)
        self.assertIsNotNone(body["multi_microstructure"])
        self.assertIn("microstructures", body["multi_microstructure"])
        self.assertIn("prices", body["multi_microstructure"])

    @_patch_network
    def test_existing_dashboard_fields_unchanged(self, *_mocks):
        """No pre-slice-D2 field was renamed or removed."""
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("stats", "btc_price", "microstructure", "windows",
                    "active_signals", "recent_trades", "equity_curve",
                    "calibration"):
            self.assertIn(key, body, f"existing key {key!r} missing")

    # --- Additional coverage for per-asset endpoint -----------------------

    def test_assets_stats_endpoint_lists_all_underlyings(self):
        r = self.client.get("/api/assets/stats")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        underlyings = {row["underlying"] for row in rows}
        for expected in ("BTC", "ETH", "SOL", "XRP"):
            self.assertIn(expected, underlyings)

    def test_mc_trades_endpoint_filters_by_status(self):
        _seed_mc_signal_and_trade(self.SessionLocal)
        r_open = self.client.get("/api/mc/trades?status=open").json()
        r_settled = self.client.get("/api/mc/trades?status=settled").json()
        self.assertEqual(len(r_open), 1)
        self.assertEqual(len(r_settled), 0)

    # --- Slice D7.5: open MC trades must stay visible in recent_trades ----

    @_patch_network
    def test_dashboard_always_includes_open_mc_trades(self, *_mocks):
        """Open MC trades older than the top-50 crypto_tech window must
        still appear in /api/dashboard.recent_trades. Settled MC trades
        follow the normal aging rules."""
        db = self.SessionLocal()
        try:
            # 60 recent BTC-tech trades. Spread over the last hour so the
            # newest 50 would naturally push anything older out.
            now = datetime.utcnow()
            for i in range(60):
                db.add(Trade(
                    market_ticker=f"btc-5m-{i}",
                    platform="polymarket",
                    event_slug=f"btc-updown-5m-{i}",
                    market_type="btc",
                    underlying_asset="BTC",
                    asset_class="crypto",
                    direction="up",
                    entry_price=0.5,
                    size=5.0,
                    timestamp=now - timedelta(minutes=i),  # newest at i=0
                    settled=False,
                    result="pending",
                    model_probability=0.55,
                    market_price_at_entry=0.50,
                    edge_at_entry=0.05,
                ))
            # Two open MC trades, a week old — would age out of any top-50
            # slice under normal ordering.
            week_ago = now - timedelta(days=7)
            for i, strike in enumerate([8000000, 8250000]):
                db.add(Trade(
                    market_ticker=f"KXBTCMAXMON-BTC-26APR30-{strike}",
                    platform="kalshi",
                    event_slug="KXBTCMAXMON-26APR30",
                    market_type="monte_carlo",
                    underlying_asset="BTC",
                    asset_class="crypto",
                    contract_style="one_touch_above",
                    direction="yes",
                    entry_price=0.19 + i * 0.1,
                    size=10.0,
                    timestamp=week_ago - timedelta(minutes=i),
                    settled=False,
                    result="pending",
                    model_probability=0.35 + i * 0.1,
                    market_price_at_entry=0.19 + i * 0.1,
                    edge_at_entry=0.06,
                ))
            db.commit()
        finally:
            db.close()

        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        recent = body["recent_trades"]

        # Union of top-50 + open MC → 52 rows (no overlap since MC is
        # a week older than any BTC row).
        self.assertEqual(len(recent), 52)

        mc_rows = [t for t in recent if t["market_type"] == "monte_carlo"]
        self.assertEqual(len(mc_rows), 2, "both open MC trades must be pinned")
        mc_tickers = {t["market_ticker"] for t in mc_rows}
        self.assertEqual(mc_tickers, {
            "KXBTCMAXMON-BTC-26APR30-8000000",
            "KXBTCMAXMON-BTC-26APR30-8250000",
        })

        # Sanity: non-MC slice is still capped at 50.
        non_mc = [t for t in recent if t["market_type"] != "monte_carlo"]
        self.assertEqual(len(non_mc), 50)

        # Sanity: sort is preserved — timestamps must be non-increasing.
        ts = [t["timestamp"] for t in recent]
        self.assertEqual(ts, sorted(ts, reverse=True))

    @_patch_network
    def test_dashboard_does_not_duplicate_mc_trades_already_in_top50(
        self, *_mocks,
    ):
        """If an open MC trade is already among the 50 most recent, the
        union must not double-include it (dedupe by id)."""
        db = self.SessionLocal()
        try:
            now = datetime.utcnow()
            # 5 recent MC trades + 10 recent BTC trades, all within the
            # last 15 minutes. Both MC rows are already in the top-50.
            for i in range(10):
                db.add(Trade(
                    market_ticker=f"btc-5m-{i}",
                    platform="polymarket",
                    event_slug=f"btc-updown-5m-{i}",
                    market_type="btc",
                    underlying_asset="BTC",
                    asset_class="crypto",
                    direction="up",
                    entry_price=0.5,
                    size=5.0,
                    timestamp=now - timedelta(seconds=i),
                    settled=False,
                    result="pending",
                    model_probability=0.55,
                    market_price_at_entry=0.50,
                    edge_at_entry=0.05,
                ))
            for i in range(5):
                db.add(Trade(
                    market_ticker=f"KXBTCMAXMON-MC-{i}",
                    platform="kalshi",
                    market_type="monte_carlo",
                    underlying_asset="BTC",
                    asset_class="crypto",
                    contract_style="one_touch_above",
                    direction="yes",
                    entry_price=0.2,
                    size=10.0,
                    timestamp=now - timedelta(seconds=i + 100),
                    settled=False,
                    result="pending",
                    model_probability=0.35,
                    market_price_at_entry=0.2,
                    edge_at_entry=0.06,
                ))
            db.commit()
        finally:
            db.close()

        recent = self.client.get("/api/dashboard").json()["recent_trades"]
        # 10 BTC + 5 MC = 15, no dedupe needed, no overflow.
        self.assertEqual(len(recent), 15)
        ids = [t["id"] for t in recent]
        self.assertEqual(len(ids), len(set(ids)), "no duplicate trade ids")


class TestDashboardServesScanCache(unittest.TestCase):
    """Slice P2: /api/dashboard must read from the scan cache populated
    by the scheduler, NOT call scan_for_signals on every request. These
    tests pin both: cache-hit reads the stored signals, and the live
    scan path is NOT called when the cache is fresh."""

    def setUp(self):
        self.client, self.SessionLocal, self._reset = _build_test_app_with_db()
        _seed_bot_state(self.SessionLocal)
        # Reset the cache so each test starts from a known state.
        import backend.core.signals as signals_mod
        signals_mod._scan_cache = None

    def tearDown(self):
        self._reset()
        import backend.core.signals as signals_mod
        signals_mod._scan_cache = None

    def _make_cached_signal(self, market_id: str, edge: float):
        """Build a TradingSignal with a distinctive market_id we can
        assert on in the response."""
        from backend.core.signals import TradingSignal
        from backend.data.crypto_markets import CryptoUpDownMarket
        now = datetime.utcnow()
        market = CryptoUpDownMarket(
            slug=f"btc-updown-5m-{market_id}",
            market_id=market_id,
            up_price=0.5, down_price=0.5,
            window_start=now, window_end=now,
            volume=100.0, volume_24h=500.0, closed=False,
        )
        return TradingSignal(
            market=market, underlying="BTC",
            edge=edge, raw_edge=edge, net_edge=edge,
            model_probability=0.55, market_probability=0.50,
            direction="up",
        )

    @patch("backend.api.main.compute_crypto_microstructure",
           new=AsyncMock(return_value=None))
    @patch("backend.api.main.fetch_crypto_price",
           new=AsyncMock(return_value=None))
    @patch("backend.api.main.fetch_active_crypto_markets",
           new=AsyncMock(return_value=[]))
    @patch("backend.api.main.scan_for_signals",
           new_callable=AsyncMock)
    def test_dashboard_serves_cached_signals_without_calling_scan(
        self, mock_scan, *_other_mocks,
    ):
        """When the cache is populated, /api/dashboard must NOT call
        scan_for_signals. The cached signal's distinctive ticker must
        appear in the response.active_signals payload."""
        from backend.core.signals import update_cached_scan
        cached = [
            self._make_cached_signal("CACHED-PROOF-A", 0.07),
            self._make_cached_signal("CACHED-PROOF-B", 0.06),
        ]
        update_cached_scan(cached)

        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        tickers = [s["market_ticker"] for s in body["active_signals"]]
        self.assertEqual(
            sorted(tickers),
            ["CACHED-PROOF-A", "CACHED-PROOF-B"],
            "dashboard must serve the cached signals, not a live scan result",
        )
        # The load-bearing assertion: scan_for_signals was NEVER called
        # during the dashboard request when cache was populated.
        mock_scan.assert_not_called()

    @patch("backend.api.main.compute_crypto_microstructure",
           new=AsyncMock(return_value=None))
    @patch("backend.api.main.fetch_crypto_price",
           new=AsyncMock(return_value=None))
    @patch("backend.api.main.fetch_active_crypto_markets",
           new=AsyncMock(return_value=[]))
    @patch("backend.api.main.scan_for_signals",
           new_callable=AsyncMock)
    def test_dashboard_falls_back_to_live_scan_on_cache_miss(
        self, mock_scan, *_other_mocks,
    ):
        """When the cache has never been populated (e.g., bot just
        restarted, scheduler hasn't fired), /api/dashboard must fall
        back to one live scan_for_signals call so the user has
        something to look at."""
        # Cache is None per setUp.
        mock_scan.return_value = []  # live fallback returns empty
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        # The fallback path was taken — scan_for_signals called exactly
        # once (the dashboard endpoint, not the scheduler).
        self.assertEqual(mock_scan.call_count, 1)


class TestKalshiSettlementParser(unittest.TestCase):
    def test_parses_yymmmdd_from_ticker(self):
        from backend.api.main import _parse_kalshi_expected_settlement
        got = _parse_kalshi_expected_settlement("KXBTCMAXMON-26APR30-8000000")
        self.assertIsNotNone(got)
        self.assertEqual((got.year, got.month, got.day), (2026, 4, 30))

    def test_returns_none_on_unparseable_ticker(self):
        from backend.api.main import _parse_kalshi_expected_settlement
        self.assertIsNone(_parse_kalshi_expected_settlement(""))
        self.assertIsNone(_parse_kalshi_expected_settlement("BTC-RANDOM"))
        # Valid YY + invalid month tag -> None, no crash
        self.assertIsNone(
            _parse_kalshi_expected_settlement("ABC-26ZZZ15-XXX"))


if __name__ == "__main__":
    unittest.main()
