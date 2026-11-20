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
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ISS = "https://iss.moex.com/iss"
BOARD = "TQCB"  # Т+: Облигации, безадресные сделки — основной режим для ВДО

SEC_COLS = "SECID,ISIN,SHORTNAME,FACEVALUE,FACEUNIT,PREVPRICE,LISTLEVEL,BONDTYPE,MATDATE,ACCRUEDINT,COUPONPERCENT,CALLOPTIONDATE,PUTOPTIONDATE,OFFERDATE"
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
            "faceunit": s.get("FACEUNIT"),
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
    """Падение от локального максимума закрытий до последнего закрытия.

    Возвращает и *время* скачка: когда был локальный максимум, когда — минимум,
    сколько минут назад это случилось (по времени биржевых свечей, МСК).
    """
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
    peak_c = max(window, key=lambda c: c["close"])
    peak = peak_c["close"]
    cur = window[-1]["close"]
    prev_day_peak = max(c["close"] for c in cs)

    # время скачка: от дневного максимума до минимума ПОСЛЕ него (тот же %, что в сообщении)
    day_peak_c = max(cs, key=lambda c: c["close"])
    day_peak_dt = _candle_dt(day_peak_c)
    after = [c for c in cs if day_peak_dt and _candle_dt(c) and _candle_dt(c) >= day_peak_dt]
    low_c = min(after, key=lambda c: c["close"]) if after else cs[-1]
    drop_pct = (prev_day_peak - low_c["close"]) / prev_day_peak * 100 if prev_day_peak else None

    now = now_msk()
    # свежесть данных: последняя свеча + её 10-минутный интервал
    last_dt = _candle_dt(cs[-1])
    data_age = None
    if last_dt:
        data_age = max(0, int((now - last_dt).total_seconds() // 60) - 10)

    return {
        "win_drop": (peak - cur) / peak * 100 if peak else None,
        "win_peak": peak,
        "cur": cur,
        "day_peak_close": prev_day_peak,
        "candles": len(cs),
        # --- тайминг ---
        "peak_time": _candle_str(day_peak_c),
        "low_time": _candle_str(low_c),
        "minutes_since_peak": _minutes_since(day_peak_dt, now),
        "minutes_since_low": _minutes_since(_candle_dt(low_c), now),
        "low_price": low_c["close"],
        "recovered_pct": (cur - low_c["close"]) / low_c["close"] * 100 if low_c["close"] else None,
        "drop_from_peak_pct": drop_pct,
        "last_data_time": _candle_str(cs[-1]),
        "data_age_min": data_age,
    }


def now_msk() -> datetime:
    """Текущее время МСК (свечи MOEX отдаёт в московском времени)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return datetime.now()


def _candle_dt(c: dict) -> datetime | None:
    """Время свечи MOEX (поле `begin`, МСК без таймзоны)."""
    raw = c.get("begin")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace(" ", "T")[:19])
    except Exception:  # noqa: BLE001
        return None


def _candle_str(c: dict) -> str | None:
    dt = _candle_dt(c)
    return dt.strftime("%H:%M") if dt else None


def _minutes_since(dt: datetime | None, now: datetime) -> int | None:
    if not dt:
        return None
    return max(0, int((now - dt).total_seconds() // 60))


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


def sane_day_high(row: dict) -> float | None:
    """Дневной максимум, очищенный от битых тиков MOEX.

    Пример реального бага: у ВТБ Б1-385 MOEX отдаёт HIGH=140,33, тогда как
    open/high/last стоят около 100 — это ошибка в данных, и из неё получается
    фиктивное «падение на 28%». Поэтому сверяем максимум с медианой цен дня:
    если он выбивается больше чем на 12% — берём максимум из open/last/low.
    """
    vals = [v for v in (row.get("open"), row.get("low"), row.get("last")) if v]
    if not vals:
        return row.get("high")
    med = sorted(vals)[len(vals) // 2]
    high = row.get("high")
    if high and med and high / med <= 1.12:
        return high
    log.info("%s: дневной максимум %.2f выглядит битым (остальные цены ~%.2f) — игнорирую",
             row.get("secid"), high or 0, med)
    return max([med] + [v for v in vals if v])


def market_breadth(snapshot: dict[str, dict]) -> dict:
    """Широта рынка: сколько бумаг в минусе, сколько упало сильно, какая медиана.

    Нужна, чтобы отличить общую панику (все падают — часто это ставка/новости,
    а не проблема выпуска) от падения одной бумаги (там важнее её собственная
    история). Считаем только ликвидные бумаги, иначе счёт размывают «мертвые»
    выпуски с одной сделкой.
    """
    moves: list[float] = []
    deep = 0
    for row in snapshot.values():
        last, prev = row.get("last"), row.get("prev")
        if not last or not prev or prev <= 0:
            continue
        if (row.get("numtrades") or 0) < 3:      # слишком тихо — не показатель
            continue
        move = (last - prev) / prev * 100
        moves.append(move)
        if move <= -2.0:
            deep += 1
    if not moves:
        return {}
    moves.sort()
    n = len(moves)
    nonzero = [m for m in moves if m] or [0.0]
    return {
        "total": n,
        "down": sum(1 for m in moves if m < 0),
        "up": sum(1 for m in moves if m > 0),
        "flat": sum(1 for m in moves if m == 0),
        "deep": deep,
        "down_share": sum(1 for m in moves if m < 0) / n * 100,
        "median": nonzero[len(nonzero) // 2],
        "worst": moves[0],
        "best": moves[-1],
    }


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
    breadth = market_breadth(snapshot)
    if breadth:
        try:
            store.set_meta("market_breadth", {**breadth, "at": now_msk().strftime("%Y-%m-%d %H:%M")})
        except Exception:  # noqa: BLE001
            pass
    moex_flags = moex_flags or {}

    out: list[dict] = []
    for secid, row in snapshot.items():
        last = row["last"]
        if not last or last <= 0:
            continue
        # ВАЖНО: у MOEX LAST уже в процентах от номинала (например 99.9 = 99,9%)
        price_pct = last

        # падение внутри дня (от максимума) и падение за окно по истории
        day_high = sane_day_high(row)
        day_drop = ((day_high - last) / day_high * 100) if day_high else 0.0
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

    # Тайминг: для сильнейших чистых кандидатов тянем 10-минутные свечи — оттуда
    # точное время скачка, сколько минут назад он был и успели ли цену откупить.
    timing_budget = int(os.environ.get("DIP_TIMING_PROBES", "8"))
    clean = [c for c in out if not c["is_bad"] and c.get("isin")]
    for cand in clean[:timing_budget]:
        if not cand.get("isin"):
            continue
        try:
            tm = intraday_drop(cand["isin"], minutes=window_minutes)
        except Exception as e:  # noqa: BLE001
            log.debug("тайминг %s: %s", cand["secid"], e)
            continue
        if tm:
            cand["timing"] = tm
            # сверка со свечами: если по ним падения от дневного пика нет — сигнал ложный
            real_drop = tm.get("drop_from_peak_pct")
            if real_drop is not None and real_drop < min_drop_pct * 0.75:
                log.info("%s: по свечам падение всего %.1f%% (в снимке %.1f%%) — ложный сигнал, пропускаю",
                         cand["secid"], real_drop, cand["drop"])
                cand["is_bad"] = True
                cand["reasons"] = [f"по биржевым свечам падения нет (всего {real_drop:.1f}%) — "
                                   f"снимок MOEX дал битую цену"]
    return out
