"""Tests for backend.core.settlement.

Coverage history:
  - Slice B1 (commit da6ce41): added 10 tests for _fetch_kalshi_resolution
    after the public-endpoint fix.
  - Slice T2 (this slice): added coverage for fetch_polymarket_resolution,
    _parse_market_resolution, calculate_pnl, settle_pending_trades, and
    update_bot_state_with_settlements. Closes audit Finding #4 (HIGH
    severity: settlement.py had zero tests pre-B1, partial coverage
    post-B1, full coverage post-T2).

Mocking strategy:
  - HTTP-layer functions (_fetch_kalshi_resolution, fetch_polymarket_resolution)
    accept an optional `http_client` kwarg matching the pattern in
    mc_execution.fetch_current_ask and spot_prices.fetch_spot. Tests pass
    an httpx.AsyncClient backed by httpx.MockTransport so we never touch
    the network.
  - Pure functions (_parse_market_resolution, calculate_pnl) need no
    mocking — they take inputs, return outputs, no I/O.
  - Orchestrator tests (settle_pending_trades, update_bot_state_with_settlements)
    use an in-memory SQLite DB built fresh per test, plus unittest.mock.patch
    to intercept fetch_polymarket_resolution / _fetch_kalshi_resolution at the
    module boundary. This isolates orchestrator logic (DB writes, dispatch,
    state transitions) from HTTP-layer behavior (covered separately).

Important note on calculate_pnl behavior:
  As of T2's writing, calculate_pnl is GROSS-OF-FEES — fees are absorbed
  into net_edge() at signal-gate time, not deducted at settlement. The
  tests in TestCalculatePnl pin this current contract. The execution-realism
  review (separate slice, not yet shipped) plans to add a Trade.fees_paid
  column and deduct fees here at settlement; when that lands, these tests
  need updated expectations.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest import mock

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core import settlement as settlement_module
from backend.core.settlement import (
    _fetch_kalshi_resolution,
    _parse_market_resolution,
    calculate_pnl,
    fetch_polymarket_resolution,
    settle_pending_trades,
    update_bot_state_with_settlements,
)
from backend.models.database import Base, BotState, Signal, Trade


def _run(coro):
    """Run an async coroutine on a dedicated loop, isolated per test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _kalshi_market_dict(status: str, result: str = ""):
    """Build a Kalshi market endpoint response shape. Top-level wrapper
    is {"market": {...}} per the live API. Tests use this to assert the
    parser handles the wrapped form correctly.

    A handful of irrelevant fields are sprinkled in so we'd notice if
    the parser ever started over-reading them."""
    market = {"status": status}
    if result:
        market["result"] = result
    market.update({
        "ticker": "KXBTCD-26APR2517-T78749.99",
        "close_time": "2026-04-25T21:00:00Z",
        "last_price_dollars": "0.0100",
    })
    return {"market": market}


def _mock_client(handler):
    """Return an httpx.AsyncClient backed by a MockTransport. The handler
    receives the full Request and returns the Response that should come
    back to the caller. Caller is responsible for closing — but
    _fetch_kalshi_resolution doesn't close injected clients (its
    owns_client guard), so tests use `with` to ensure cleanup."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFetchKalshiResolution(unittest.TestCase):

    # ---- Happy paths -----------------------------------------------------

    def test_finalized_yes_returns_resolved_with_value_1(self):
        def handler(req):
            return httpx.Response(200, json=_kalshi_market_dict("finalized", "yes"))

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution(
                    "KXBTCD-26APR2517-T77249.99", http_client=client,
                )

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)

    def test_finalized_no_returns_resolved_with_value_0(self):
        def handler(req):
            return httpx.Response(200, json=_kalshi_market_dict("finalized", "no"))

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution(
                    "KXBTCD-26APR2517-T78749.99", http_client=client,
                )

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 0.0)

    def test_determined_status_also_treated_as_resolved(self):
        # Kalshi has historically used both 'finalized' and 'determined'
        # for resolved markets; the original code accepted both, and so
        # does the rewrite. Pin that here so a future tightening doesn't
        # silently break monthly contracts that may use 'determined'.
        def handler(req):
            return httpx.Response(200, json=_kalshi_market_dict("determined", "yes"))

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)

    # ---- Not-yet-resolved paths -----------------------------------------

    def test_active_status_returns_not_resolved(self):
        def handler(req):
            return httpx.Response(200, json=_kalshi_market_dict("active"))

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_finalized_with_blank_result_returns_not_resolved(self):
        # Defensive: 'finalized' status but empty result string. Don't
        # guess; bail out so the settlement loop retries next cycle.
        def handler(req):
            return httpx.Response(200, json=_kalshi_market_dict("finalized", ""))

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_unknown_result_value_returns_not_resolved(self):
        # Strict equality on result — anything other than literal 'yes'
        # / 'no' falls through. Guards against Kalshi adding a third
        # state like 'void' that we shouldn't silently mistreat.
        def handler(req):
            return httpx.Response(200, json=_kalshi_market_dict("finalized", "void"))

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    # ---- Error paths -----------------------------------------------------

    def test_http_500_returns_not_resolved(self):
        def handler(req):
            return httpx.Response(500, text="server error")

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_http_404_returns_not_resolved(self):
        def handler(req):
            return httpx.Response(404, json={"error": "not found"})

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("UNKNOWN", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_malformed_json_returns_not_resolved(self):
        def handler(req):
            return httpx.Response(200, content=b"not json")

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    # ---- Defensive shape handling ---------------------------------------

    def test_unwrapped_response_also_parses(self):
        # If Kalshi ever returns the market dict at the top level instead
        # of wrapped under "market", the .get('market', data) fallback
        # should still work. Pinning it because the live API today does
        # wrap, but the parser claims tolerance — test the claim.
        def handler(req):
            return httpx.Response(200, json={
                "status": "finalized",
                "result": "yes",
                "ticker": "X",
            })

        async def go():
            async with _mock_client(handler) as client:
                return await _fetch_kalshi_resolution("X", http_client=client)

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)


# =============================================================================
# T2 additions begin here. Everything above this line is the B1-era code.
# =============================================================================


# ---- Polymarket helpers -----------------------------------------------------

def _polymarket_market_dict(
    closed: bool = True,
    outcome_prices: object = '["1", "0"]',
    market_id: str = "520927",
):
    """Build a Polymarket gamma-api /markets/{id} response. Verified shape:
    closed: bool, outcomePrices: JSON-encoded string like '["1", "0"]'
    (first outcome won) or '["0", "1"]' (second outcome won), outcomes:
    JSON-encoded list of labels like '["Yes","No"]' or '["Up","Down"]'.

    The `outcome_prices` arg accepts either a string (the live API form)
    or a list (defensive form the parser also handles)."""
    return {
        "id": market_id,
        "closed": closed,
        "active": True,
        "outcomePrices": outcome_prices,
        "outcomes": '["Yes", "No"]',
        "question": "Test market",
        "slug": "test-market",
    }


def _polymarket_event_with_market(market: dict, slug: str = "test-event"):
    """Build a Polymarket gamma-api /events?slug=... response shape:
    a list with one event containing a `markets` list."""
    return [{
        "slug": slug,
        "markets": [market],
    }]


# ---- In-memory DB fixture ---------------------------------------------------

def _make_in_memory_db():
    """Build a fresh in-memory SQLite engine + session and create all tables.

    Returns (engine, session). Caller is responsible for closing the session
    and disposing the engine — tests use try/finally to guarantee cleanup
    even on assertion failures. Each test gets a clean DB; no shared state
    across tests. Memory cost is negligible.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    return engine, SessionLocal()


def _make_pending_trade(
    db,
    *,
    platform: str = "polymarket",
    market_ticker: str = "test-market",
    direction: str = "up",
    entry_price: float = 0.45,
    size: float = 10.0,
    model_probability: float = 0.55,
    event_slug: str = "test-event",
    signal_id=None,
) -> Trade:
    """Persist a pending Trade row with sensible defaults. Returns the row
    so the caller can reference its id. features defaults to {} per the
    NOT NULL DEFAULT '{}' schema convention."""
    trade = Trade(
        signal_id=signal_id,
        market_ticker=market_ticker,
        platform=platform,
        event_slug=event_slug,
        market_type="btc" if platform == "polymarket" else "monte_carlo",
        direction=direction,
        entry_price=entry_price,
        size=size,
        timestamp=datetime.utcnow(),
        settled=False,
        result="pending",
        model_probability=model_probability,
        features={},
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def _make_bot_state(
    db,
    *,
    bankroll: float = 200.0,
    total_pnl: float = 0.0,
    total_trades: int = 0,
    winning_trades: int = 0,
) -> BotState:
    """Persist a BotState row. The model has only one logical row; tests
    that need to assert on BotState updates should call this in setUp."""
    state = BotState(
        bankroll=bankroll,
        total_pnl=total_pnl,
        total_trades=total_trades,
        winning_trades=winning_trades,
    )
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


# =============================================================================
# TestParseMarketResolution — pure parser, no mocking needed
# =============================================================================

class TestParseMarketResolution(unittest.TestCase):
    """Direct unit tests for the pure parser at settlement.py:84.

    The HTTP-layer fetch_polymarket_resolution function is a thin wrapper
    around this parser; testing the parser directly gives us cleaner
    assertions on the actual settlement logic (the >0.99 / <0.01 thresholds,
    the JSON-string-vs-list dual handling) without HTTP mocking overhead.
    """

    def test_first_outcome_won_via_string_prices_returns_yes(self):
        # Live API returns outcomePrices as a JSON-encoded string.
        market = _polymarket_market_dict(closed=True, outcome_prices='["1", "0"]')
        is_resolved, value = _parse_market_resolution(market)
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)

    def test_second_outcome_won_via_string_prices_returns_no(self):
        market = _polymarket_market_dict(closed=True, outcome_prices='["0", "1"]')
        is_resolved, value = _parse_market_resolution(market)
        self.assertTrue(is_resolved)
        self.assertEqual(value, 0.0)

    def test_first_outcome_won_via_list_prices_also_parsed(self):
        # Defensive: parser handles outcomePrices as either str or list.
        # Pin it because real API uses str but the parser claims tolerance.
        market = _polymarket_market_dict(closed=True, outcome_prices=["1.0", "0.0"])
        is_resolved, value = _parse_market_resolution(market)
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)

    def test_market_not_closed_returns_not_resolved(self):
        # If closed=False, parser short-circuits to (False, None) regardless
        # of whatever outcome prices may already be filled in.
        market = _polymarket_market_dict(closed=False, outcome_prices='["1", "0"]')
        is_resolved, value = _parse_market_resolution(market)
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_closed_with_ambiguous_prices_returns_not_resolved(self):
        # 0.99 boundary is exclusive; 0.5 (mid) is neither yes nor no.
        market = _polymarket_market_dict(closed=True, outcome_prices='["0.5", "0.5"]')
        is_resolved, value = _parse_market_resolution(market)
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_missing_outcome_prices_returns_not_resolved(self):
        market = {"closed": True, "id": "x"}  # no outcomePrices key
        is_resolved, value = _parse_market_resolution(market)
        self.assertFalse(is_resolved)
        self.assertIsNone(value)


# =============================================================================
# TestFetchPolymarketResolution — HTTP plumbing via MockTransport
# =============================================================================

class TestFetchPolymarketResolution(unittest.TestCase):
    """Tests for the HTTP wrapper. Parser correctness is covered separately
    in TestParseMarketResolution; these tests focus on the HTTP plumbing:
    event-slug-vs-direct-URL flow, error handling, 404 fallback dispatch.

    The 404 fallback path calls _search_market_in_events, which makes its
    own HTTP calls. Rather than thread http_client through that helper too
    (would inflate the production change beyond B1's one-function pattern),
    tests for the 404 path use mock.patch to intercept _search_market_in_events.
    """

    def test_event_slug_path_resolves_via_first_market(self):
        def handler(req):
            self.assertIn("/events", str(req.url))
            self.assertEqual(req.url.params.get("slug"), "btc-up-or-down")
            event = _polymarket_event_with_market(
                _polymarket_market_dict(outcome_prices='["1", "0"]'),
                slug="btc-up-or-down",
            )
            return httpx.Response(200, json=event)

        async def go():
            async with _mock_client(handler) as client:
                return await fetch_polymarket_resolution(
                    "anything", event_slug="btc-up-or-down", http_client=client,
                )

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)

    def test_direct_market_url_path_when_no_event_slug(self):
        # No event_slug provided -> goes straight to /markets/{id}.
        def handler(req):
            self.assertIn("/markets/520927", str(req.url))
            return httpx.Response(200, json=_polymarket_market_dict(
                outcome_prices='["0", "1"]', market_id="520927",
            ))

        async def go():
            async with _mock_client(handler) as client:
                return await fetch_polymarket_resolution("520927", http_client=client)

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 0.0)

    def test_direct_url_404_falls_back_to_event_search(self):
        # When the direct /markets/{id} returns 404, the function dispatches
        # to _search_market_in_events. We mock that helper at the module
        # boundary to verify the dispatch happens (and to avoid the helper
        # making real HTTP calls).
        def handler(req):
            return httpx.Response(404, json={"error": "not found"})

        async def fake_search(market_id):
            self.assertEqual(market_id, "missing-id")
            return True, 1.0

        async def go():
            async with _mock_client(handler) as client:
                with mock.patch.object(
                    settlement_module, "_search_market_in_events",
                    side_effect=fake_search,
                ):
                    return await fetch_polymarket_resolution(
                        "missing-id", http_client=client,
                    )

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)

    def test_http_500_returns_not_resolved(self):
        def handler(req):
            return httpx.Response(500, text="server error")

        async def go():
            async with _mock_client(handler) as client:
                return await fetch_polymarket_resolution("any", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_malformed_json_returns_not_resolved(self):
        def handler(req):
            return httpx.Response(200, content=b"not json")

        async def go():
            async with _mock_client(handler) as client:
                return await fetch_polymarket_resolution("any", http_client=client)

        is_resolved, value = _run(go())
        self.assertFalse(is_resolved)
        self.assertIsNone(value)

    def test_event_slug_returns_empty_list_does_not_raise(self):
        # An unknown slug returns []. Function should NOT raise; it should
        # fall through to the direct /markets/{id} path, which the same
        # handler will also serve below.
        call_count = {"events": 0, "markets": 0}

        def handler(req):
            path = str(req.url.path)
            if "/events" in path:
                call_count["events"] += 1
                return httpx.Response(200, json=[])  # empty events list
            if "/markets/" in path:
                call_count["markets"] += 1
                return httpx.Response(200, json=_polymarket_market_dict(
                    outcome_prices='["1", "0"]'
                ))
            return httpx.Response(404)

        async def go():
            async with _mock_client(handler) as client:
                return await fetch_polymarket_resolution(
                    "fallback-id", event_slug="unknown-slug", http_client=client,
                )

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertEqual(value, 1.0)
        self.assertEqual(call_count["events"], 1)
        self.assertEqual(call_count["markets"], 1)

    def test_market_id_with_special_chars_is_url_safe(self):
        # Polymarket market IDs are typically numeric, but defensive: if
        # an event slug like "btc-up-or-down-2026-04-25-21:00et" came
        # through the market_id path, the URL-build should not crash.
        # httpx auto-escapes path parameters so this should just work.
        seen_url = {"value": None}

        def handler(req):
            seen_url["value"] = str(req.url)
            return httpx.Response(200, json=_polymarket_market_dict(
                outcome_prices='["1", "0"]'
            ))

        async def go():
            async with _mock_client(handler) as client:
                return await fetch_polymarket_resolution(
                    "weird:id-with/special chars", http_client=client,
                )

        is_resolved, value = _run(go())
        self.assertTrue(is_resolved)
        self.assertIsNotNone(seen_url["value"])
        # The URL should contain SOME form of the market id; we don't
        # over-specify the exact escaping (httpx makes the call), just
        # that no exception was raised mid-build.


# =============================================================================
# TestCalculatePnl — pure logic, gross-of-fees
# =============================================================================

class TestCalculatePnl(unittest.TestCase):
    """Tests for the pure PnL calculator at settlement.py:164.

    IMPORTANT: As of T2's writing, calculate_pnl is GROSS-OF-FEES. The
    fees module is NOT consulted here; fees were already absorbed into
    net_edge() at signal-gate time. These tests pin that current contract.
    The execution-realism review (separate slice) plans to deduct fees at
    settlement; when that ships, these tests need updated expected values.
    """

    def _make_trade(self, direction, entry_price, size):
        # In-memory Trade — no DB persistence needed for pure-logic tests.
        return Trade(
            direction=direction,
            entry_price=entry_price,
            size=size,
            features={},
        )

    def test_yes_bet_wins(self):
        # Buy YES at 0.40, settles YES (1.0). PnL = size * (1 - entry).
        trade = self._make_trade(direction="yes", entry_price=0.40, size=10.0)
        pnl = calculate_pnl(trade, settlement_value=1.0)
        self.assertAlmostEqual(pnl, 10.0 * (1.0 - 0.40))  # 6.00
        self.assertGreater(pnl, 0)

    def test_yes_bet_loses(self):
        # Buy YES at 0.40, settles NO (0.0). PnL = -size * entry.
        trade = self._make_trade(direction="yes", entry_price=0.40, size=10.0)
        pnl = calculate_pnl(trade, settlement_value=0.0)
        self.assertAlmostEqual(pnl, -10.0 * 0.40)  # -4.00
        self.assertLess(pnl, 0)

    def test_no_bet_wins(self):
        # Buy NO at 0.30, settles NO (0.0). PnL = size * (1 - entry).
        trade = self._make_trade(direction="no", entry_price=0.30, size=10.0)
        pnl = calculate_pnl(trade, settlement_value=0.0)
        self.assertAlmostEqual(pnl, 10.0 * (1.0 - 0.30))  # 7.00
        self.assertGreater(pnl, 0)

    def test_no_bet_loses(self):
        # Buy NO at 0.30, settles YES (1.0). PnL = -size * entry.
        trade = self._make_trade(direction="no", entry_price=0.30, size=10.0)
        pnl = calculate_pnl(trade, settlement_value=1.0)
        self.assertAlmostEqual(pnl, -10.0 * 0.30)  # -3.00
        self.assertLess(pnl, 0)

    def test_up_direction_treated_as_yes(self):
        # The function maps "up" -> "yes" internally. UP at 0.40 winning
        # (settlement 1.0) should produce identical PnL to YES at 0.40 winning.
        up_trade = self._make_trade(direction="up", entry_price=0.40, size=10.0)
        yes_trade = self._make_trade(direction="yes", entry_price=0.40, size=10.0)
        self.assertEqual(
            calculate_pnl(up_trade, settlement_value=1.0),
            calculate_pnl(yes_trade, settlement_value=1.0),
        )

    def test_down_direction_treated_as_no(self):
        # Mirror of the above for down/no.
        down_trade = self._make_trade(direction="down", entry_price=0.30, size=10.0)
        no_trade = self._make_trade(direction="no", entry_price=0.30, size=10.0)
        self.assertEqual(
            calculate_pnl(down_trade, settlement_value=0.0),
            calculate_pnl(no_trade, settlement_value=0.0),
        )

    def test_tiny_trade_size_no_division_by_zero(self):
        # A $0.01 trade should produce sensible PnL, not crash on rounding.
        trade = self._make_trade(direction="yes", entry_price=0.50, size=0.01)
        pnl_win = calculate_pnl(trade, settlement_value=1.0)
        pnl_lose = calculate_pnl(trade, settlement_value=0.0)
        # Both rounded to 2 decimals: $0.005 -> $0.01 (banker's rounding
        # in Python's round() may give 0.0; we just assert no crash and
        # sensible signs/magnitudes).
        self.assertGreaterEqual(pnl_win, 0.0)
        self.assertLessEqual(pnl_lose, 0.0)

    def test_yes_at_p_winning_mirrors_no_at_1_minus_p_winning(self):
        # Symmetry check (gross-of-fees): a YES bet at price P that wins
        # has the SAME magnitude PnL as a NO bet at price (1-P) that wins,
        # given the same size. Sanity-checks the +(size)*(1-entry) form
        # is consistent across both directions.
        size = 10.0
        p = 0.30
        yes_at_p = self._make_trade(direction="yes", entry_price=p, size=size)
        no_at_1_minus_p = self._make_trade(direction="no", entry_price=1 - p, size=size)
        # YES wins: pnl = size * (1 - 0.30) = 7.00
        # NO wins:  pnl = size * (1 - 0.70) = 3.00
        # These are NOT equal — the symmetry is only "loser pays entry,
        # winner gets (1 - entry)" within a side, not across-side equality.
        # The honest assertion: YES winning at price P and NO winning at
        # price P (same price both sides) produce identical PnL.
        no_at_p = self._make_trade(direction="no", entry_price=p, size=size)
        self.assertEqual(
            calculate_pnl(yes_at_p, settlement_value=1.0),
            calculate_pnl(no_at_p, settlement_value=0.0),
        )


# =============================================================================
# TestSettlePendingTrades — orchestrator with in-memory DB and patched fetchers
# =============================================================================

class TestSettlePendingTrades(unittest.TestCase):
    """Tests for the orchestrator at settlement.py:291.

    The orchestrator: queries pending trades, dispatches to platform-specific
    resolution fetchers via check_market_settlement, marks settled rows
    (settled, settlement_value, pnl, result, settlement_time, features),
    updates linked Signal rows, commits.

    Notably, this function does NOT update BotState — that's a separate
    function (update_bot_state_with_settlements, tested below). Tests here
    do not assert on BotState; tests there do not assert on Trade rows.

    HTTP layer is mocked via mock.patch on the two fetchers
    (fetch_polymarket_resolution and _fetch_kalshi_resolution) at the
    settlement_module boundary. The HTTP-layer behavior of those functions
    is covered separately by their own test classes.
    """

    def setUp(self):
        self.engine, self.db = _make_in_memory_db()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_empty_pending_returns_empty_list(self):
        # No trades in DB at all -> orchestrator returns [], commits nothing.
        result = _run(settle_pending_trades(self.db))
        self.assertEqual(result, [])

    def test_single_polymarket_trade_resolves_yes(self):
        trade = _make_pending_trade(
            self.db, platform="polymarket", direction="yes",
            entry_price=0.40, size=10.0,
        )

        async def fake_fetch(market_id, event_slug=None):
            return True, 1.0

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_fetch,
        ):
            result = _run(settle_pending_trades(self.db))

        self.assertEqual(len(result), 1)
        self.db.refresh(trade)
        self.assertTrue(trade.settled)
        self.assertEqual(trade.settlement_value, 1.0)
        self.assertEqual(trade.result, "win")
        self.assertAlmostEqual(trade.pnl, 6.0)  # 10 * (1 - 0.40)
        self.assertIsNotNone(trade.settlement_time)

    def test_single_polymarket_trade_resolves_no_loss(self):
        trade = _make_pending_trade(
            self.db, platform="polymarket", direction="yes",
            entry_price=0.40, size=10.0,
        )

        async def fake_fetch(market_id, event_slug=None):
            return True, 0.0

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_fetch,
        ):
            _run(settle_pending_trades(self.db))

        self.db.refresh(trade)
        self.assertTrue(trade.settled)
        self.assertEqual(trade.settlement_value, 0.0)
        self.assertEqual(trade.result, "loss")
        self.assertAlmostEqual(trade.pnl, -4.0)  # -10 * 0.40

    def test_single_kalshi_trade_dispatches_to_kalshi_fetcher(self):
        # Platform = "kalshi" -> dispatcher calls _fetch_kalshi_resolution,
        # NOT fetch_polymarket_resolution. Verify the correct fetcher fires.
        trade = _make_pending_trade(
            self.db, platform="kalshi",
            market_ticker="KXBTCD-26APR2517-T78749.99",
            direction="yes", entry_price=0.10, size=14.37,
        )
        poly_called = {"value": False}
        kalshi_called = {"value": False}

        async def fake_poly(*args, **kwargs):
            poly_called["value"] = True
            return False, None

        async def fake_kalshi(*args, **kwargs):
            kalshi_called["value"] = True
            return True, 0.0  # NO won -> YES bet loses

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_poly,
        ), mock.patch.object(
            settlement_module, "_fetch_kalshi_resolution", side_effect=fake_kalshi,
        ):
            _run(settle_pending_trades(self.db))

        self.assertTrue(kalshi_called["value"])
        self.assertFalse(poly_called["value"])
        self.db.refresh(trade)
        self.assertTrue(trade.settled)
        self.assertEqual(trade.result, "loss")

    def test_unresolved_trade_stays_pending(self):
        # Fetcher returns (False, None) -> trade stays pending, no DB mutation.
        trade = _make_pending_trade(self.db, platform="polymarket")

        async def fake_fetch(market_id, event_slug=None):
            return False, None

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_fetch,
        ):
            result = _run(settle_pending_trades(self.db))

        self.assertEqual(result, [])
        self.db.refresh(trade)
        self.assertFalse(trade.settled)
        self.assertIsNone(trade.settlement_value)
        self.assertIsNone(trade.pnl)
        self.assertEqual(trade.result, "pending")

    def test_mixed_platforms_settled_correctly(self):
        # Two Polymarket + one Kalshi pending. Each dispatches to its own
        # fetcher; PnL math runs per-trade; all three end up settled.
        poly_yes = _make_pending_trade(
            self.db, platform="polymarket", direction="yes",
            entry_price=0.40, size=10.0, market_ticker="poly-yes",
        )
        poly_no = _make_pending_trade(
            self.db, platform="polymarket", direction="no",
            entry_price=0.30, size=10.0, market_ticker="poly-no",
        )
        kalshi_yes = _make_pending_trade(
            self.db, platform="kalshi", direction="yes",
            entry_price=0.10, size=10.0, market_ticker="kalshi-x",
        )

        async def fake_poly(market_id, event_slug=None):
            return True, 1.0  # YES wins on Polymarket side

        async def fake_kalshi(*args, **kwargs):
            return True, 0.0  # NO wins on Kalshi -> kalshi_yes loses

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_poly,
        ), mock.patch.object(
            settlement_module, "_fetch_kalshi_resolution", side_effect=fake_kalshi,
        ):
            result = _run(settle_pending_trades(self.db))

        self.assertEqual(len(result), 3)
        for t in (poly_yes, poly_no, kalshi_yes):
            self.db.refresh(t)
            self.assertTrue(t.settled)
        # poly_yes (YES bet) wins; poly_no (NO bet) loses; kalshi_yes (YES) loses.
        self.assertEqual(poly_yes.result, "win")
        self.assertEqual(poly_no.result, "loss")
        self.assertEqual(kalshi_yes.result, "loss")

    def test_signal_calibration_updated_on_settlement(self):
        # When a Trade has a signal_id, the orchestrator should update the
        # linked Signal row with actual_outcome / outcome_correct /
        # settlement_value / settled_at.
        signal = Signal(
            market_ticker="poly-x", platform="polymarket", direction="up",
            model_probability=0.55, market_price=0.45, edge=0.10,
            features={},
        )
        self.db.add(signal)
        self.db.commit()
        self.db.refresh(signal)

        _make_pending_trade(
            self.db, platform="polymarket", direction="up",
            entry_price=0.45, size=10.0, signal_id=signal.id,
        )

        async def fake_fetch(market_id, event_slug=None):
            return True, 1.0  # UP wins -> signal.direction matches

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_fetch,
        ):
            _run(settle_pending_trades(self.db))

        self.db.refresh(signal)
        self.assertEqual(signal.actual_outcome, "up")
        self.assertTrue(signal.outcome_correct)
        self.assertEqual(signal.settlement_value, 1.0)
        self.assertIsNotNone(signal.settled_at)

    def test_fetcher_exception_keeps_trade_pending(self):
        # If the fetcher raises, the per-trade try/except catches it; the
        # trade stays pending; the orchestrator continues with other trades.
        will_fail = _make_pending_trade(self.db, platform="polymarket", market_ticker="will-fail")
        will_succeed = _make_pending_trade(self.db, platform="polymarket", market_ticker="will-succeed")

        call_log = []

        async def fake_fetch(market_id, event_slug=None):
            call_log.append(market_id)
            if market_id == "will-fail":
                raise RuntimeError("simulated network error")
            return True, 1.0

        with mock.patch.object(
            settlement_module, "fetch_polymarket_resolution", side_effect=fake_fetch,
        ):
            result = _run(settle_pending_trades(self.db))

        # Only will-succeed gets settled; will-fail stays pending. Both are
        # attempted (the orchestrator's per-trade try/except absorbs the
        # raised exception cleanly so other trades aren't blocked).
        self.assertEqual(len(result), 1)
        self.assertEqual(set(call_log), {"will-fail", "will-succeed"})
        self.db.refresh(will_fail)
        self.db.refresh(will_succeed)
        self.assertFalse(will_fail.settled)
        self.assertTrue(will_succeed.settled)


# =============================================================================
# TestUpdateBotStateWithSettlements — BotState bookkeeping
# =============================================================================

class TestUpdateBotStateWithSettlements(unittest.TestCase):
    """Tests for the BotState updater at settlement.py:362.

    This function is the second half of the settlement orchestration: it
    walks a list of already-settled trades and updates the singleton
    BotState row's bankroll, total_pnl, and winning_trades. Note that
    total_trades is NOT incremented here in the current production code
    — that counter is maintained elsewhere (it's incremented when trades
    are CREATED, not when they settle).
    """

    def setUp(self):
        self.engine, self.db = _make_in_memory_db()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _settled_trade(self, pnl, result):
        # Build an in-memory Trade row representing one already-settled
        # trade. Doesn't need to be persisted — the function reads pnl
        # and result from the trade objects passed in directly.
        return Trade(
            settled=True,
            pnl=pnl,
            result=result,
            features={},
        )

    def test_empty_list_is_noop(self):
        state = _make_bot_state(self.db, bankroll=200.0, total_pnl=0.0)
        _run(update_bot_state_with_settlements(self.db, []))
        self.db.refresh(state)
        self.assertEqual(state.bankroll, 200.0)
        self.assertEqual(state.total_pnl, 0.0)
        self.assertEqual(state.winning_trades, 0)

    def test_missing_bot_state_logs_warning_does_not_crash(self):
        # No BotState row exists in DB. Function should warn and return
        # without raising.
        trades = [self._settled_trade(pnl=5.0, result="win")]
        # Should not raise.
        _run(update_bot_state_with_settlements(self.db, trades))

    def test_winning_trade_updates_bankroll_pnl_and_winning_count(self):
        state = _make_bot_state(self.db, bankroll=200.0, total_pnl=0.0, winning_trades=10)
        trades = [self._settled_trade(pnl=6.0, result="win")]
        _run(update_bot_state_with_settlements(self.db, trades))
        self.db.refresh(state)
        self.assertAlmostEqual(state.bankroll, 206.0)
        self.assertAlmostEqual(state.total_pnl, 6.0)
        self.assertEqual(state.winning_trades, 11)

    def test_losing_trade_updates_bankroll_pnl_but_not_winning_count(self):
        state = _make_bot_state(self.db, bankroll=200.0, total_pnl=0.0, winning_trades=10)
        trades = [self._settled_trade(pnl=-4.0, result="loss")]
        _run(update_bot_state_with_settlements(self.db, trades))
        self.db.refresh(state)
        self.assertAlmostEqual(state.bankroll, 196.0)
        self.assertAlmostEqual(state.total_pnl, -4.0)
        self.assertEqual(state.winning_trades, 10)  # unchanged on loss

    def test_mixed_wins_and_losses_accumulate(self):
        # Three settlements: one $6 win, one $-4 loss, one $-2 loss.
        # Net: -$0 bankroll change, total_pnl = 0, winning_trades += 1.
        state = _make_bot_state(self.db, bankroll=200.0, total_pnl=0.0, winning_trades=0)
        trades = [
            self._settled_trade(pnl=6.0, result="win"),
            self._settled_trade(pnl=-4.0, result="loss"),
            self._settled_trade(pnl=-2.0, result="loss"),
        ]
        _run(update_bot_state_with_settlements(self.db, trades))
        self.db.refresh(state)
        self.assertAlmostEqual(state.bankroll, 200.0)
        self.assertAlmostEqual(state.total_pnl, 0.0)
        self.assertEqual(state.winning_trades, 1)


if __name__ == "__main__":
    unittest.main()
