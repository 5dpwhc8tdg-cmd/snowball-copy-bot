"""Проверка гипотезы «резкое падение ВДО → отскок через пару часов».

Методологически важные детали:
  * вход считаем по ЦЕНЕ ЗАКРЫТИЯ свечи, в которой случилось падение
    (а не по минимуму — минимум часто одна сделка, «отскок» от него фиктивен);
  * одно событие на бумагу не чаще, чем раз в 2 часа (иначе одно падение
    считается десятки раз и статистика врёт);
  * результат — изменение цены к +30 мин, +1 ч, +2 ч и к концу дня,
    плюс «максимум за 2 часа» как оценка лучшего исхода.

Запуск: python3 research_dips.py --days 14 --drop 4
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
        out.append({
            "close": rec["close"], "low": rec["low"], "high": rec["high"],
            "value": rec.get("value") or 0, "volume": rec.get("volume") or 0,
            "begin": datetime.fromisoformat(rec["begin"]),
        })
    return out


def _fwd(cs, j, minutes):
    """Ближайшая свеча не позже чем через `minutes` от cs[j]."""
    target = cs[j]["begin"] + timedelta(minutes=minutes)
    best = None
    for c in cs[j + 1:]:
        if c["begin"] > target:
            break
        best = c
    return best


def find_events(cs: list[dict], drop_pct: float, window: int, cooldown: int = 120) -> list[dict]:
    events: list[dict] = []
    last_event_ts = None
    for j in range(20, len(cs)):
        if last_event_ts and (cs[j]["begin"] - last_event_ts).total_seconds() < cooldown * 60:
            continue
        window_slice = cs[max(0, j - window // 10):j]
        if not window_slice:
            continue
        peak = max(c["close"] for c in window_slice)
        entry = cs[j]["close"]
        drop = (peak - entry) / peak * 100
        if drop < drop_pct:
            continue

        prev_vols = [c["volume"] for c in cs[max(0, j - 20):j] if c["volume"]]
        med_vol = statistics.median(prev_vols) if prev_vols else 0
        vol_ratio = (cs[j]["volume"] / med_vol) if med_vol else None

        delayed_entry = cs[j + 1]["close"] if j + 1 < len(cs) else None
        out = {"ticker": None, "ts": cs[j]["begin"], "peak": peak, "entry": entry,
               "delayed_entry": delayed_entry,
               "drop_pct": drop, "vol_ratio": vol_ratio, "value": cs[j]["value"]}
        if delayed_entry:
            c = _fwd(cs, j + 1, 120)
            out["ret_2h_delayed"] = ((c["close"] - delayed_entry) / delayed_entry * 100) if c else None
        else:
            out["ret_2h_delayed"] = None
        for label, minutes in (("30m", 30), ("1h", 60), ("2h", 120)):
            c = _fwd(cs, j, minutes)
            out[f"ret_{label}"] = ((c["close"] - entry) / entry * 100) if c else None
        # максимум и минимум в следующие 2 часа
        fut = [c for c in cs[j + 1:] if (c["begin"] - cs[j]["begin"]).total_seconds() <= 7200]
        if fut:
            out["max_2h"] = (max(c["high"] for c in fut) - entry) / entry * 100
            out["min_2h"] = (min(c["low"] for c in fut) - entry) / entry * 100
        else:
            out["max_2h"] = out["min_2h"] = None
        eod = [c for c in cs[j + 1:] if c["begin"].date() == cs[j]["begin"].date()]
        out["ret_eod"] = ((eod[-1]["close"] - entry) / entry * 100) if eod else None
        events.append(out)
        last_event_ts = cs[j]["begin"]
    return events


def describe(name: str, xs: list[float]) -> str:
    if not xs:
        return f"{name}: нет данных"
    pos = sum(1 for x in xs if x > 0) / len(xs) * 100
    return (f"{name}: n={len(xs):3d} | медиана {statistics.median(xs):+.2f}% | "
            f"среднее {statistics.mean(xs):+.2f}% | в плюсе {pos:.0f}% | "
            f"лучшие 25% ≥ {sorted(xs)[int(len(xs) * 0.75)]:+.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--drop", type=float, default=4.0)
    ap.add_argument("--window", type=int, default=60, help="за сколько минут ищем падение")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    rows = api.fetch_all_transactions("ukbRjaXjfg", max_pages=4, page_size=100)
    tickers = list(dict.fromkeys(r["ticker"] for r in rows))[: args.limit]
    print(f"бумаг: {len(tickers)} | период: {args.days} дн. | падение ≥{args.drop}% за ≤{args.window} мин")
    print("вход = закрытие свечи падения (реальная цена), не минимум\n")

    events = []
    for n, t in enumerate(tickers, 1):
        cs = candles(t, args.days)
        if len(cs) < 30:
            continue
        for e in find_events(cs, args.drop, args.window):
            e["ticker"] = t
            events.append(e)
        if n % 10 == 0:
            print(f"  ... {n}/{len(tickers)}, событий: {len(events)}")
        time.sleep(0.15)

    print(f"\nсобытий всего: {len(events)}")
    if not events:
        return 0

    r2 = [e["ret_2h"] for e in events if e["ret_2h"] is not None]
    re_ = [e["ret_eod"] for e in events if e["ret_eod"] is not None]
    mx = [e["max_2h"] for e in events if e["max_2h"] is not None]
    mn = [e["min_2h"] for e in events if e["min_2h"] is not None]
    print(describe("через 30 мин", [e["ret_30m"] for e in events if e["ret_30m"] is not None]))
    print(describe("через 1 час ", [e["ret_1h"] for e in events if e["ret_1h"] is not None]))
    print(describe("через 2 часа", r2))
    print(describe("к концу дня ", re_))
    print(describe("макс. за 2ч  ", mx))
    print(describe("мин. за 2ч   ", mn))
    print(describe("вход через 10 мин (+2ч)", [e["ret_2h_delayed"] for e in events if e.get("ret_2h_delayed") is not None]))

    # насколько результат зависит от объёма в момент падения (шум vs реальность)
    for label, lo, hi in (("объём вялый (ratio<1)", None, 1.0), ("объём x1-3", 1.0, 3.0), ("объём x3+", 3.0, None)):
        sub = [e["ret_2h"] for e in events
               if e["ret_2h"] is not None and e["vol_ratio"] is not None
               and (lo is None or e["vol_ratio"] >= lo) and (hi is None or e["vol_ratio"] < hi)]
        print(describe(label, sub))

    json.dump([{**e, "ts": e["ts"].isoformat()} for e in events],
              open("dip_events.json", "w"), ensure_ascii=False, indent=1)
    events.sort(key=lambda e: e["ts"], reverse=True)
    print("\nсвежие 10:")
    for e in events[:10]:
        print(f"  {e['ts']:%d.%m %H:%M} {e['ticker']:14} −{e['drop_pct']:4.1f}% "
              f"vol×{(e['vol_ratio'] or 0):4.1f} | 30м {e['ret_30m'] if e['ret_30m'] is None else round(e['ret_30m'],1)} "
              f"| 2ч {round(e['ret_2h'],1) if e['ret_2h'] is not None else '-'} "
              f"| max2ч {round(e['max_2h'],1) if e['max_2h'] is not None else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
