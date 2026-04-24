"""Calibration-adjusted Kelly sizing (Change 3)."""
import logging
import time
from sqlalchemy import text
from backend.config import settings
from backend.models.database import SessionLocal

logger = logging.getLogger("trading_bot")
CALIBRATION_MIN = 0.1
_CACHE_TTL_SECONDS = 300
_cache = {}


def _compute_raw_multiplier(mt):
    db = SessionLocal()
    try:
        q = text("SELECT COUNT(*), AVG(ABS(edge_at_entry)), AVG(pnl / NULLIF(size, 0)) FROM trades WHERE settled = 1 AND market_type = :mt AND edge_at_entry IS NOT NULL AND size > 0")
        r = db.execute(q, {"mt": mt}).fetchone()
    finally:
        db.close()
    if r is None:
        return 1.0, {"num_trades": 0, "reason": "no_data"}
    n, pe, re_v = r
    n = n or 0
    pe = pe or 0.0
    re_v = re_v or 0.0
    info = {"num_trades": n, "predicted_edge_avg": pe, "realized_edge_avg": re_v}
    if n < settings.MIN_TRADES_FOR_CALIBRATION:
        info["reason"] = "insufficient_data"
        return 1.0, info
    if abs(pe) < 0.001:
        info["reason"] = "predicted_edge_near_zero"
        return 1.0, info
    if re_v <= 0:
        info["reason"] = "realized_edge_non_positive"
        return CALIBRATION_MIN, info
    raw = re_v / pe
    info["reason"] = "calibrated"
    info["raw_multiplier"] = raw
    return raw, info


def get_calibration_multiplier(mt="btc"):
    now = time.time()
    c = _cache.get(mt)
    if c is not None:
        m, ts, _ = c
        if now - ts < _CACHE_TTL_SECONDS:
            return m
    try:
        raw, info = _compute_raw_multiplier(mt)
    except Exception as e:
        logger.warning(f"Calibration failed for {mt}: {e}")
        _cache[mt] = (1.0, now, {"reason": "error"})
        return 1.0
    clamped = max(CALIBRATION_MIN, min(settings.CALIBRATION_MAX_MULTIPLIER, raw))
    info["clamped_multiplier"] = clamped
    _cache[mt] = (clamped, now, info)
    n = info.get("num_trades", 0)
    if n >= settings.MIN_TRADES_FOR_CALIBRATION:
        pe_val = info["predicted_edge_avg"]
        re_val = info["realized_edge_avg"]
        logger.info(f"[CALIBRATION] {mt}: n={n} predicted={pe_val:.2%} realized={re_val:.2%} raw={raw:.3f} clamped={clamped:.3f}")
    return clamped


def get_calibration_info(mt="btc"):
    get_calibration_multiplier(mt)
    c = _cache.get(mt)
    if c is None:
        return {"num_trades": 0, "reason": "no_cache"}
    m, ts, info = c
    return {**info, "cached_multiplier": m}


def clear_cache():
    _cache.clear()
