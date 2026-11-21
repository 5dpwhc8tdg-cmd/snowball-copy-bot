"""Сбор истории MOEX для проверки правил сканера на прошлом (без ожидания).

Данные:
  * дневная история TQCB (`/iss/history/.../TQCB/securities.json`) — LOW/HIGH/CLOSE,
    объём, число сделок, дата погашения, номинал. Есть за любой прошлый день;
  * 10-минутные свечи по бумаге (`/candles.json?interval=10`) — чтобы померить
    отскок внутри дня (тот горизонт, на котором работает бот).

Кэш: `data/hist/daily-<YYYY-MM-DD>.json`, `data/hist/candles-<SECID>.json`.
Повторный анализ не требует сети.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ISS = "https://iss.moex.com/iss"
BOARD = "TQCB"
CACHE = Path(os.environ.get("HIST_CACHE", "data/hist"))

DAILY_COLS = ("SECID,SHORTNAME,NUMTRADES,VALUE,LOW,HIGH,CLOSE,WAPRICE,ACCINT,"
              "YIELDCLOSE,MATDATE,FACEVALUE,FACEUNIT,CURRENCYID,LISTLEVEL,BONDTYPE,"
              "COUPONPERCENT,OFFERDATE,DURATION,TRADEDATE")


def _get(url: str, timeout: int = 60, retries: int = 3) -> dict | None:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (snowball-bot)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                print(f"  ! не удалось: {e}")
                return None
            time.sleep(1.5 * (i + 1))
    return None


def trading_days(days_back: int) -> list[str]:
    """Последние `days_back` календарных дней, кроме выходных (праздники отсеются пустыми)."""
    today = datetime.now().date()
    out = []
    d = today
    while len(out) < days_back:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def fetch_day(day: str, refresh: bool = False) -> list[dict]:
    """Все бумаги TQCB за один торговый день (постранично, параметр date=)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"daily-{day}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())

    rows: list[dict] = []
    start = 0
    while True:
        url = (f"{ISS}/history/engines/stock/markets/bonds/boards/{BOARD}/securities.json"
               f"?iss.meta=off&date={day}&start={start}")
        data = _get(url)
        if not data or "history" not in data:
            break
        block = data["history"]
        cols = block.get("columns") or []
        chunk = [dict(zip(cols, r)) for r in block.get("data") or []]
        rows.extend(chunk)
        if len(chunk) < 100:
            break
        start += 100
        if start > 6000:
            break
    path.write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def fetch_candles(secid: str, frm: str, till: str, interval: int = 10,
                  refresh: bool = False) -> list[dict]:
    """Свечи по бумаге за период (постранично, 500 строк на страницу)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"candles-{secid}-{interval}-{frm}-{till}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())

    out: list[dict] = []
    start = 0
    while True:
        url = (f"{ISS}/engines/stock/markets/bonds/securities/{secid}/candles.json"
               f"?iss.meta=off&interval={interval}&from={frm}&till={till}&start={start}")
        data = _get(url, timeout=30)
        if not data or "candles" not in data:
            break
        block = data["candles"]
        cols = block.get("columns") or []
        chunk = [dict(zip(cols, r)) for r in block.get("data") or []]
        out.extend(chunk)
        if len(chunk) < 500:
            break
        start += 500
    path.write_text(json.dumps(out, ensure_ascii=False))
    return out


if __name__ == "__main__":
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    days = trading_days(n)
    print(f"качаю {len(days)} дней: {days[0]} … {days[-1]}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_day, d): d for d in days}
        for f in as_completed(futs):
            done += 1
            d = futs[f]
            try:
                rows = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! {d}: {e}", flush=True)
                continue
            with_close = [r for r in rows if r.get("CLOSE")]
            print(f"  {d}: бумаг {len(rows):5d}, с закрытием {len(with_close):5d} "
                  f"({done}/{len(days)})", flush=True)


def fetch_bond_history(secid: str, frm: str, till: str, refresh: bool = False) -> list[dict]:
    """Вся дневная история одной бумаги за период (постранично).

    Для 3-летнего анализа «что покупает владелец»: один запрос на бумагу
    отдаёт все её дни (цена, доходность, дюрация, купон, оборот).
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"bond-{secid}-{frm}-{till}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    out: list[dict] = []
    start = 0
    while True:
        url = (f"{ISS}/history/engines/stock/markets/bonds/securities/{secid}.json"
               f"?iss.meta=off&from={frm}&till={till}&start={start}")
        data = _get(url)
        if not data or "history" not in data:
            break
        block = data["history"]
        cols = block.get("columns") or []
        chunk = [dict(zip(cols, r)) for r in block.get("data") or []]
        out.extend(chunk)
        if len(chunk) < 100:
            break
        start += 100
    path.write_text(json.dumps(out, ensure_ascii=False))
    return out


def fetch_index(secid: str, frm: str, till: str, refresh: bool = False) -> list[dict]:
    """История индекса (RGBI — индекс гособлигаций, база для альфы)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"index-{secid}-{frm}-{till}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    out: list[dict] = []
    start = 0
    while True:
        url = (f"{ISS}/history/engines/stock/markets/index/securities/{secid}.json"
               f"?iss.meta=off&from={frm}&till={till}&start={start}")
        data = _get(url)
        if not data or "history" not in data:
            break
        block = data["history"]
        cols = block.get("columns") or []
        chunk = [dict(zip(cols, r)) for r in block.get("data") or []]
        out.extend(chunk)
        if len(chunk) < 100:
            break
        start += 100
    path.write_text(json.dumps(out, ensure_ascii=False))
    return out
