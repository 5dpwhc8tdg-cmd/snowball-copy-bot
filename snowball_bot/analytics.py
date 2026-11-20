"""Расширенная аналитика по бумаге — то, что помогает решить «брать или нет».

Собирается из MOEX ISS (без токена):
  * купон: ставка, частота, ближайшая выплата и сколько дней до неё;
  * оферта (put/call) и дюрация — когда можно выйти и насколько бумага чувствительна к ставке;
  * история: максимум/минимум за 52 недели, текущая просадка, сколько раз бумага
    уже падала на ≥4% и как отскакивала (её личная статистика, а не средняя по рынку);
  * ликвидность: средний дневной оборот и число сделок за последние дни;
  * «честная» проверка отскока: после такого же падения в прошлом цена выше через 2 часа?
"""
from __future__ import annotations

import json
import logging
import statistics
import time
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

ISS = "https://iss.moex.com/iss"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; snowball-copy-bot/1.0)", "Accept": "application/json"}


def _get(url: str, timeout: int = 30, retries: int = 2) -> dict | None:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                log.debug("ISS %s: %s", url, e)
                return None
            time.sleep(1.2)
    return None


def _block(url: str, name: str) -> list[dict]:
    data = _get(url)
    if not data or name not in data:
        return []
    block = data[name]
    cols = block.get("columns") or []
    return [dict(zip(cols, r)) for r in block.get("data") or []]


def daily_candles(isin: str, days: int = 180) -> list[dict]:
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _block(f"{ISS}/engines/stock/markets/bonds/securities/{isin}/candles.json"
                  f"?iss.meta=off&interval=24&from={frm}", "candles")
    return [r for r in rows if r.get("close")]


def coupon_info(isin: str) -> dict:
    """Купоны и амортизация по графику MOEX."""
    rows = _block(f"{ISS}/securities/{isin}/bondization.json?iss.meta=off", "coupons")
    amort = _block(f"{ISS}/securities/{isin}/bondization.json?iss.meta=off", "amortizations")
    now = datetime.now()
    future = [r for r in rows if r.get("coupondate") and r["coupondate"] != "0000-00-00"]
    future = sorted(future, key=lambda r: r["coupondate"])
    nxt = next((r for r in future if r["coupondate"] >= now.strftime("%Y-%m-%d")), None)
    out: dict = {
        "coupon_count": len(rows),
        "amort_count": len(amort),
    }
    if nxt:
        try:
            days_to = (datetime.fromisoformat(nxt["coupondate"]) - now).days
        except Exception:
            days_to = None
        out.update({
            "next_coupon_date": nxt["coupondate"],
            "next_coupon_days": days_to,
            "next_coupon_value": nxt.get("value"),
            "next_coupon_pct": nxt.get("valueprc"),
        })
    if rows:
        out["coupon_period_days"] = rows[-1].get("couponperiod")
    if amort:
        upcoming = [r for r in amort if r.get("amortdate") and r["amortdate"] >= now.strftime("%Y-%m-%d")]
        out["amort_upcoming"] = len(upcoming)
        if upcoming:
            out["next_amort_date"] = sorted(upcoming, key=lambda r: r["amortdate"])[0]["amortdate"]
    return out


def board_info(isin: str) -> dict:
    """Дюрация, оферта, режим торгов — из описания бумаги."""
    rows = _block(f"{ISS}/securities/{isin}.json?iss.meta=off", "description")
    d = {r.get("name"): r.get("value") for r in rows}
    return {
        "offerdate": d.get("OFFERDATE"),
        "calloption": d.get("CALLOPTIONDATE"),
        "putoption": d.get("PUTOPTIONDATE"),
        "has_prospectus": d.get("HASPROSPECTUS"),
        "days_to_redemption": d.get("DAYSTOREDEMPTION"),
        "coupon_details": d.get("COUPON_DETAILS"),
        "bond_subtype": d.get("BOND_SUBTYPE"),
        "faceunit": d.get("FACEUNIT"),
        "facevalue": d.get("FACEVALUE"),
        "lotsize": d.get("LOTSIZE"),
    }


def liquidity_and_history(isin: str, drop_threshold: float = 4.0, days: int = 180) -> dict:
    """Ликвидность за последние дни + личная статистика падений бумаги."""
    rows = daily_candles(isin, days)
    if not rows:
        return {}
    closes = [r["close"] for r in rows]
    vols = [r.get("value") or 0 for r in rows]
    last = closes[-1]
    last5 = vols[-5:]
    out = {
        "avg_value_5d": statistics.mean(last5) if last5 else 0,
        "max_52w": max(closes),
        "min_52w": min(closes),
        "drawdown_from_max_pct": (last - max(closes)) / max(closes) * 100 if max(closes) else None,
        "days_history": len(rows),
    }
    # сколько раз за период бумага падала на drop_threshold% от локального максимума
    # и что было через 2 дня (аналог «двух часов» для дневного масштаба)
    drops = []
    for i in range(1, len(rows)):
        peak = max(closes[max(0, i - 5):i])
        if not peak:
            continue
        drop = (peak - closes[i]) / peak * 100
        if drop >= drop_threshold:
            fwd = closes[i + 2] if i + 2 < len(closes) else None
            drops.append((drop, ((fwd - closes[i]) / closes[i] * 100) if fwd else None))
    if drops:
        fwd_all = [d[1] for d in drops if d[1] is not None]
        out["prev_drops"] = len(drops)
        if fwd_all:
            out["prev_drops_rebound_median_pct"] = statistics.median(fwd_all)
            out["prev_drops_up_share_pct"] = sum(1 for x in fwd_all if x > 0) / len(fwd_all) * 100
    return out


def full_profile(isin: str, store=None, ttl: int = 86400) -> dict:
    """Всё сразу, с кэшем на сутки (в диске/state)."""
    key = f"profile:{isin}"
    if store is not None:
        cached = store.get_asset(key, ttl=ttl)
        if cached:
            return cached
    prof: dict = {}
    prof.update(coupon_info(isin))
    prof.update(board_info(isin))
    prof.update(liquidity_and_history(isin))
    if store is not None and prof:
        store.put_asset(key, prof)
    return prof
