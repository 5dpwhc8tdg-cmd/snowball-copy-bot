"""Исследование: сколько отскока остаётся, если сигнал приходит с опозданием.

Вопрос от пользователя: «сигналы и так приходят поздно, стоит ли сразу
отрицать такие сделки?». Проверяем на истории MOEX:

  для каждого события «падение ≥4% за 60 мин» берём цену в момент падения,
  затем цены через 0/10/20/30/45/60/90/120 минут ПОСЛЕ него и считаем:
    * сколько отскока осталось от точки входа (это то, что реально можно взять);
    * сколько «съедено» (отскок от дна до момента входа);
    * какой максимальный отскок был ПОСЛЕ входа — то есть был ли смысл входить;
    * как часто вход с опозданием всё ещё даёт плюс к концу дня.

Отдельно — зависимость от «глубины» и объёма, и разбивка по бакетам задержки.

Запуск: python3 research_lag.py --days 21 --drop 4 --limit 40
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from datetime import datetime, timedelta

from snowball_bot import api

ISS = "https://iss.moex.com/iss"
LAGS = (0, 10, 20, 30, 45, 60, 90, 120)


def curl_json(url: str, retries: int = 3):
    for _ in range(retries):
        p = subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True)
        try:
            return json.loads(p.stdout)
        except Exception:
            time.sleep(1.5)
    return {}


def candles(isin: str, days: int) -> list[dict]:
    frm = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    d = curl_json(f"{ISS}/engines/stock/markets/bonds/securities/{isin}/candles.json"
                  f"?iss.meta=off&interval=10&from={frm}")
    block = d.get("candles") or {}
    cols = block.get("columns") or []
    out = []
    for row in block.get("data") or []:
        rec = dict(zip(cols, row))
        if not rec.get("close"):
            continue
        out.append({"close": rec["close"], "low": rec["low"], "high": rec["high"],
                    "value": rec.get("value") or 0, "volume": rec.get("volume") or 0,
                    "begin": datetime.fromisoformat(rec["begin"])})
    return out


def _at(cs, j, minutes):
    """Свеча, ближайшая к моменту j+minutes (не позже)."""
    target = cs[j]["begin"] + timedelta(minutes=minutes)
    best = None
    for c in cs[j + 1:]:
        if c["begin"] > target:
            break
        best = c
    return best


def events_for(cs: list[dict], drop_pct: float, window: int = 60, cooldown: int = 120) -> list[dict]:
    out = []
    last = None
    for j in range(20, len(cs)):
        if last and (cs[j]["begin"] - last).total_seconds() < cooldown * 60:
            continue
        win = cs[max(0, j - window // 10):j]
        if not win:
            continue
        peak = max(c["close"] for c in win)
        entry0 = cs[j]["close"]
        drop = (peak - entry0) / peak * 100
        if drop < drop_pct:
            continue
        # дно после падения (по low) и последующий отскок
        fut = [c for c in cs[j + 1:] if (c["begin"] - cs[j]["begin"]).total_seconds() <= 4 * 3600]
        if len(fut) < 3:
            continue
        bottom_c = min(fut, key=lambda c: c["low"])
        bottom = bottom_c["low"]
        max_after = max(c["high"] for c in fut)
        eod = [c for c in cs[j + 1:] if c["begin"].date() == cs[j]["begin"].date()]

        row = {
            "ts": cs[j]["begin"], "peak": peak, "entry0": entry0, "drop": drop,
            "bottom": bottom,
            "bottom_min": (bottom_c["begin"] - cs[j]["begin"]).total_seconds() / 60,
            "max_after_pct": (max_after - entry0) / entry0 * 100,
            "eod": ((eod[-1]["close"] - entry0) / entry0 * 100) if eod else None,
            "lag": {},
        }
        for lag in LAGS:
            c = cs[j] if lag == 0 else _at(cs, j, lag)
            if not c:
                continue
            entry = c["close"]
            # отскок от дна до момента входа (уже упущено)
            recovered = (entry - bottom) / bottom * 100
            # что осталось взять: максимум ПОСЛЕ входа в следующие 2 часа
            fut2 = [x for x in cs if x["begin"] > c["begin"]
                    and (x["begin"] - c["begin"]).total_seconds() <= 7200]
            max_left = ((max(x["high"] for x in fut2) - entry) / entry * 100) if fut2 else None
            eod_left = None
            if eod:
                later = [x for x in eod if x["begin"] > c["begin"]]
                if later:
                    eod_left = (later[-1]["close"] - entry) / entry * 100
            row["lag"][lag] = {"entry": entry, "recovered": recovered,
                               "max_left": max_left, "eod_left": eod_left}
        out.append(row)
        last = cs[j]["begin"]
    return out


def describe(name: str, xs: list[float]) -> str:
    if not xs:
        return f"{name}: нет данных"
    pos = sum(1 for x in xs if x > 0) / len(xs) * 100
    s = sorted(xs)
    return (f"{name:34} n={len(xs):3d} | медиана {statistics.median(xs):+6.2f}% | "
            f"среднее {statistics.mean(xs):+6.2f}% | в плюсе {pos:3.0f}% | "
            f"p25 {s[int(len(s) * .25)]:+6.2f}% | p75 {s[int(len(s) * .75)]:+6.2f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--drop", type=float, default=4.0)
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    rows = api.fetch_all_transactions("ukbRjaXjfg", max_pages=4, page_size=100)
    tickers = list(dict.fromkeys(r["ticker"] for r in rows))[: args.limit]
    print(f"бумаг: {len(tickers)} | {args.days} дн. | падение ≥{args.drop}% за 60 мин")
    print("вход = закрытие свечи в момент (t+лаг), «осталось» = максимум за 2 ч ПОСЛЕ входа\n")

    events: list[dict] = []
    for n, t in enumerate(tickers, 1):
        cs = candles(t, args.days)
        if len(cs) < 40:
            continue
        for e in events_for(cs, args.drop):
            e["ticker"] = t
            events.append(e)
        if n % 10 == 0:
            print(f"  ... {n}/{len(tickers)}, событий: {len(events)}")
        time.sleep(0.1)

    print(f"\nсобытий: {len(events)}\n")
    if not events:
        return 0

    print("=== ОСТАЛОСЬ ВЗЯТЬ (макс. отскок за 2 ч после входа) ===")
    for lag in LAGS:
        xs = [e["lag"][lag]["max_left"] for e in events
              if lag in e["lag"] and e["lag"][lag]["max_left"] is not None]
        print(describe(f"вход через {lag:3d} мин", xs))

    print("\n=== УЖЕ УПУЩЕНО (отскок от дна до момента входа) ===")
    for lag in LAGS:
        xs = [e["lag"][lag]["recovered"] for e in events if lag in e["lag"]]
        print(describe(f"через {lag:3d} мин", xs))

    print("\n=== К КОНЦУ ДНЯ от момента входа ===")
    for lag in LAGS:
        xs = [e["lag"][lag]["eod_left"] for e in events
              if lag in e["lag"] and e["lag"][lag]["eod_left"] is not None]
        print(describe(f"вход через {lag:3d} мин", xs))

    # где «дно» по времени: сколько минут от сигнала до минимума
    mins = [e["bottom_min"] for e in events if e["bottom_min"] is not None]
    if mins:
        s = sorted(mins)
        print(f"\nдно наступает через: медиана {statistics.median(s):.0f} мин "
              f"(p25 {s[int(len(s)*.25)]:.0f}, p75 {s[int(len(s)*.75)]:.0f})")

    # насколько глубоко ещё падало после входа (риск позднего входа)
    print("\n=== РИСК: сколько ещё падало после входа (ниже цены входа, %) ===")
    for lag in (0, 30, 60):
        xs = []
        for e in events:
            if lag not in e["lag"]:
                continue
            entry = e["lag"][lag]["entry"]
            xs.append((e["bottom"] - entry) / entry * 100)
        print(describe(f"вход через {lag:3d} мин", xs))

    # разбивка по величине падения
    print("\n=== ЗАВИСИТ ЛИ ОТ ГЛУБИНЫ ПАДЕНИЯ (вход через 30 мин) ===")
    for lo, hi in ((4, 6), (6, 9), (9, 100)):
        xs = [e["lag"][30]["max_left"] for e in events
              if e["drop"] >= lo and e["drop"] < hi and 30 in e["lag"]
              and e["lag"][30]["max_left"] is not None]
        print(describe(f"падение {lo}-{hi if hi < 100 else '+'}%", xs))

    json.dump([{**e, "ts": e["ts"].isoformat(), "lag": {str(k): v for k, v in e["lag"].items()}}
               for e in events], open("lag_events.json", "w"), ensure_ascii=False, indent=1)
    print("\nсохранено в lag_events.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
