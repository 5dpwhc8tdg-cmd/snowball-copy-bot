"""Сканер «паники» на рынке облигаций MOEX — ловит провалы без задержки.

Идея: не ждать, пока владелец портфеля опубликует сделку (это +5–30 минут),
а смотреть сам рынок. Один запрос к MOEX ISS отдаёт снимок всех облигаций
основного режима (~3000 бумаг, 5–8 секунд), дальше — сравнение с предыдущим
снимком и отсев «плохих» ситуаций.

История снимков хранится в state (JSON) — так работают и GitHub Actions
(каждый запуск отдельный), и постоянный процесс.

Фильтры «плохой ситуации» (то, что не надо покупать в падении):
  * на MOEX зафиксирован дефолт или технический дефолт;
  * цена ниже MIN_PRICE_PCT от номинала (рынок уже оценил выпуск как проблемный);
  * падение «на воздухе»: почти нет сделок/оборота в моменте;
  * падение от исторического максимума ≤ 60% (бумага в глубоком даунтренде).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ISS = "https://iss.moex.com/iss"
BOARD = "TQCB"  # Т+: Облигации, безадресные сделки — основной режим для ВДО

SEC_COLS = "SECID,ISIN,SHORTNAME,FACEVALUE,PREVPRICE,LISTLEVEL,BONDTYPE,MATDATE,ACCRUEDINT,COUPONPERCENT,CALLOPTIONDATE,PUTOPTIONDATE,OFFERDATE"
MD_COLS = ("SECID,LAST,HIGH,LOW,OPEN,LASTCHANGEPRCNT,BID,OFFER,VALTODAY,NUMTRADES,"
           "UPDATETIME,YIELD,DURATION,CLOSEPRICE,WAPRICE")


def _get(url: str, timeout: int = 40, retries: int = 2) -> dict | None:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (snowball-copy-bot)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                log.warning("MOEX-снимок не получен: %s", e)
                return None
            time.sleep(2)
    return None


def fetch_snapshot() -> dict[str, dict]:
    """Один запрос — все облигации основного режима. Ключ: SECID."""
    url = (f"{ISS}/engines/stock/markets/bonds/boards/{BOARD}/securities.json"
           f"?iss.meta=off&iss.only=securities,marketdata"
           f"&securities.columns={SEC_COLS}&marketdata.columns={MD_COLS}")
    data = _get(url)
    if not data:
        return {}
    sec_block = data.get("securities") or {}
    md_block = data.get("marketdata") or {}
    if not sec_block.get("data"):
        return {}
    sec_cols = sec_block["columns"]
    md_cols = md_block["columns"]
    secs = {row[sec_cols.index("SECID")]: dict(zip(sec_cols, row)) for row in sec_block["data"]}
    out: dict[str, dict] = {}
    for row in md_block.get("data") or []:
        md = dict(zip(md_cols, row))
        sid = md.get("SECID")
        s = secs.get(sid)
        if not s or not md.get("LAST"):
            continue
        out[sid] = {
            "secid": sid,
            "isin": s.get("ISIN"),
            "name": s.get("SHORTNAME"),
            "facevalue": s.get("FACEVALUE"),
            "prev": s.get("PREVPRICE"),
            "listlevel": s.get("LISTLEVEL"),
            "bondtype": s.get("BONDTYPE"),
            "matdate": s.get("MATDATE"),
            "nkd": s.get("ACCRUEDINT"),
            "coupon_pct": s.get("COUPONPERCENT"),
            "last": md.get("LAST"),
            "high": md.get("HIGH"),
            "low": md.get("LOW"),
            "open": md.get("OPEN"),
            "bid": md.get("BID"),
            "offer": md.get("OFFER"),
            "valtoday": md.get("VALTODAY") or 0,
            "numtrades": md.get("NUMTRADES") or 0,
            "yield": md.get("YIELD"),
            "upd": md.get("UPDATETIME"),
        }
    return out


# ---------------------------------------------------------------------------
# Падение за последние N минут — по 10-минутным свечам MOEX (без раздувания state)
# ---------------------------------------------------------------------------
def intraday_drop(isin: str, minutes: int = 30) -> dict | None:
    """Падение от локального максимума закрытий до последнего закрытия."""
    today = datetime.now().strftime("%Y-%m-%d")
    url = (f"{ISS}/engines/stock/markets/bonds/securities/{isin}/candles.json"
           f"?iss.meta=off&interval=10&from={today}")
    data = _get(url, timeout=25, retries=1)
    if not data:
        return None
    block = data.get("candles") or {}
    cols = block.get("columns") or []
    rows = block.get("data") or []
    if len(rows) < 2:
        return None
    cs = [dict(zip(cols, r)) for r in rows if dict(zip(cols, r)).get("close")]
    if len(cs) < 2:
        return None
    # окно: свечи, попавшие в последние `minutes` минут (интервал 10 мин → N/10 свечей, минимум 2)
    n = max(2, minutes // 10)
    window = cs[-n:]
    peak = max(c["close"] for c in window)
    cur = window[-1]["close"]
    prev_day_peak = max(c["close"] for c in cs)
    return {
        "win_drop": (peak - cur) / peak * 100 if peak else None,
        "win_peak": peak,
        "cur": cur,
        "day_peak_close": prev_day_peak,
        "candles": len(cs),
    }


# ---------------------------------------------------------------------------
# История снимков (нужна только при запуске постоянным процессом)
# ---------------------------------------------------------------------------
HIST_PREFIX = "dips:hist:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_history(store, snapshot: dict[str, dict], keep_minutes: int = 240) -> dict:
    """Сохраняет срез цен (secid → last + время) и подчищает старое."""
    hist = store.get_asset(HIST_PREFIX + "all", ttl=10 ** 9) or {}
    series: dict[str, list] = hist.get("series") or {}
    now = datetime.now(timezone.utc)
    edge = now - timedelta(minutes=keep_minutes)
    for secid, row in snapshot.items():
        points = series.setdefault(secid, [])
        points.append([_now_iso(), row["last"]])
        series[secid] = [p for p in points if _parse(p[0]) >= edge][-80:]
    # выкидываем бумаги, по которым давно нет данных
    series = {k: v for k, v in series.items() if v and _parse(v[-1][0]) >= edge}
    store.put_asset(HIST_PREFIX + "all", {"series": series})
    return series


def _parse(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.now(timezone.utc)


def drop_since(series: list, minutes: int) -> tuple[float | None, float | None, int]:
    """Падение от максимума за последние `minutes` минут до текущей цены."""
    if not series:
        return None, None, 0
    now = _parse(series[-1][0])
    edge = now - timedelta(minutes=minutes)
    window = [p for p in series if _parse(p[0]) >= edge]
    if len(window) < 2:
        return None, None, len(window)
    peak = max(p[1] for p in window)
    cur = window[-1][1]
    if not peak:
        return None, None, len(window)
    return (peak - cur) / peak * 100, peak, len(window)


# ---------------------------------------------------------------------------
# Главная функция: найти интересные провалы
# ---------------------------------------------------------------------------
def find_dips(
    store,
    moex_flags: dict[str, dict] | None = None,
    min_drop_pct: float = 4.0,
    window_minutes: int = 30,
    day_drop_pct: float = 3.0,
    min_price_pct: float = 70.0,
    min_trades: int = 5,
    min_value: float = 30_000,
    max_series_points: int | None = None,
) -> list[dict]:
    """Возвращает список кандидатов, отсортированный по величине падения."""
    snapshot = fetch_snapshot()
    if not snapshot:
        return []
    series_all = record_history(store, snapshot)
    moex_flags = moex_flags or {}

    out: list[dict] = []
    for secid, row in snapshot.items():
        last = row["last"]
        if not last or last <= 0:
            continue
        # ВАЖНО: у MOEX LAST уже в процентах от номинала (например 99.9 = 99,9%)
        price_pct = last

        # падение внутри дня (от максимума) и падение за окно по истории
        day_drop = ((row["high"] - last) / row["high"] * 100) if row.get("high") else 0.0
        win_drop, peak, points = drop_since(series_all.get(secid, []), window_minutes)

        drop = max(day_drop, win_drop or 0.0)
        if drop < min_drop_pct:
            continue
        # если внутридневного падения нет — требуем более сильный сигнал по истории
        if day_drop < day_drop_pct and (win_drop or 0) < min_drop_pct:
            continue

        reasons: list[str] = []
        bad = False
        flags = moex_flags.get(row["isin"]) or {}
        if flags.get("has_default"):
            reasons.append("на MOEX зафиксирован дефолт")
            bad = True
        if flags.get("has_tech_default"):
            reasons.append("технический дефолт на MOEX")
            bad = True
        if price_pct is not None and price_pct < min_price_pct:
            reasons.append(f"цена {price_pct:.0f}% от номинала — рынок уже считает выпуск проблемным")
            bad = True
        if row["numtrades"] < min_trades:
            reasons.append(f"всего {row['numtrades']} сделок — падение «на воздухе»")
            bad = True
        if row["valtoday"] < min_value:
            reasons.append(f"оборот за день {row['valtoday']:,.0f} ₽ — слишком тихо".replace(",", " "))
            bad = True
        # спред: если бид/оффер разъехались, цена «бумажная»
        bid, offer = row.get("bid"), row.get("offer")
        if bid and offer and bid > 0 and offer > 0:
            spread_pct = (offer - bid) / bid * 100
            if spread_pct > 6:
                reasons.append(f"спред {spread_pct:.1f}% — выйти по нормальной цене не получится")
                bad = True

        out.append({
            **row,
            "price_pct": price_pct,
            "day_drop": day_drop,
            "win_drop": win_drop,
            "peak": peak,
            "drop": drop,
            "points": points,
            "is_bad": bad,
            "reasons": reasons,
            "flags": flags,
        })
    out.sort(key=lambda x: -x["drop"])
    return out
