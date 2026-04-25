"""Tests for backend.core.settlement.

Slice B1: focused tests for _fetch_kalshi_resolution after the public-
endpoint fix. The function was previously bailing on credentials
(silent failure leaving every MC trade pending forever); these tests
pin the new contract — a direct httpx GET against the public Kalshi
market endpoint, with strict 'yes'/'no' parsing and fail-safe
(False, None) returns on every error path.

Audit T2 noted that test_settlement.py didn't exist. This slice is
scoped to the B1 bug fix; broader coverage of calculate_pnl,
check_market_settlement, etc. is a separate slice.

Mocking strategy: _fetch_kalshi_resolution accepts an optional
http_client kwarg (B1 added the seam, matching the same pattern in
mc_execution.fetch_current_ask). Tests pass an httpx.AsyncClient with
an httpx.MockTransport that returns canned responses, so we never
make real network calls.
"""
from __future__ import annotations

import asyncio
import unittest

import httpx

from backend.core.settlement import _fetch_kalshi_resolution


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


if __name__ == "__main__":
    unittest.main()
