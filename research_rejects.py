"""Разбор: почему отсеянные «НЕ БРАТЬ» всё равно дали плюс — и что с этим делать.

Считаем не средние по всем, а по каждой причине отсева отдельно, и сравниваем
с рынком за тот же день (медиана движения всех ликвидных бумаг). Идея: если
рынок в этот день вырос на 1%, то «+1% у отсеянных» — это не мастерство
фильтра, а бета.

Запуск: python3 research_rejects.py [--state state/state.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime

from snowball_bot import analytics, dipscanner, verdict as V


def intraday(isin: str, day: str) -> list[dict]:
    url = (f"{analytics.ISS}/engines/stock/markets/bonds/securities/{isin}/candles.json"
           f"?iss.meta=off&interval=10&from={day}&till={day}")
    rows = analytics._block(url, "candles")
    out = []
    for r in rows:
        if not r.get("close") or not r.get("begin"):
            continue
        out.append({"dt": datetime.fromisoformat(str(r["begin"]).replace(" ", "T")[:19]),
                    "close": r["close"], "high": r.get("high"), "low": r.get("low")})
    return out


def daily_move(isin: str, day: str) -> float | None:
    """Движение за день в % (последняя цена к первой) — для «сколько дал рынок»."""
    cs = intraday(isin, day)
    if len(cs) < 2:
        return None
    return (cs[-1]["close"] - cs[0]["close"]) / cs[0]["close"] * 100


def evaluate(entry: dict) -> dict | None:
    isin, day, t = entry.get("isin"), entry.get("day"), entry.get("time") or "10:00"
    if not isin or not day:
        return None
    cs = intraday(isin, day)
    if len(cs) < 3:
        return None
    hh, mm = (int(x) for x in t.split(":")) if ":" in t else (10, 0)
    sig = cs[0]["dt"].replace(hour=hh, minute=mm, second=0)
    after = [c for c in cs if c["dt"] >= sig]
    if len(after) < 2:
        return None
    entry_px = after[0]["close"]

    def at(minutes):
        target = sig.timestamp() + minutes * 60
        w = [c for c in cs if c["dt"].timestamp() <= target]
        return w[-1]["close"] if w else None

    r30, r60, r120 = at(30), at(60), at(120)
    eod = cs[-1]["close"]
    return {
        "r30": ((r30 - entry_px) / entry_px * 100) if r30 else None,
        "r60": ((r60 - entry_px) / entry_px * 100) if r60 else None,
        "r120": ((r120 - entry_px) / entry_px * 100) if r120 else None,
        "eod": (eod - entry_px) / entry_px * 100,
        "entry_px": entry_px,
        "low_after": (min(c["low"] or c["close"] for c in after) - entry_px) / entry_px * 100,
        "day_move": (cs[-1]["close"] - cs[0]["close"]) / cs[0]["close"] * 100,
        "day2": None,
    }


def day2_close(isin: str, day: str) -> float | None:
    rows = analytics.daily_candles(isin, days=10)
    later = [r for r in rows if r.get("close") and str(r.get("begin") or "")[:10] > day]
    return later[0]["close"] if later else None


def stat(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    xs_sorted = sorted(xs)
    pos = sum(1 for x in xs if x > 0) / len(xs) * 100
    return (f"n={len(xs):3d} | медиана {statistics.median(xs):+6.2f}% | "
            f"среднее {statistics.mean(xs):+6.2f}% | в плюсе {pos:3.0f}% | "
            f"p25 {xs_sorted[int(len(xs_sorted)*.25)]:+6.2f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/state.json")
    args = ap.parse_args()
    data = json.load(open(args.state, encoding="utf-8"))["meta"]
    journal = data.get("dip_journal", {})
    breadth = data.get("market_breadth", {})
    entries = []
    for day, items in journal.items():
        for e in items:
            entries.append({**e, "day": day})
    print(f"сигналов в журнале: {len(entries)}\n")

    # восстановим, за что именно отсеяли: гоняем те же правила на сохранённых данных
    rows = []
    profiles_cache: dict[str, dict] = {}
    for e in entries:
        isin = e.get("isin")
        if not isin:
            continue
        if isin not in profiles_cache:
            try:
                profiles_cache[isin] = analytics.full_profile(isin)
            except Exception:
                profiles_cache[isin] = {}
        prof = profiles_cache[isin]
        fake = {"price_pct": e.get("price_pct"), "drop": e.get("drop"), "valtoday": 0,
                "faceunit": prof.get("faceunit") or "SUR", "nkd": e.get("nkd"),
                "facevalue": e.get("facevalue"), "timing": {"minutes_since_low": None}}
        v = V.decide(fake, prof, breadth, None)
        res = evaluate(e)
        if not res:
            continue
        d2 = day2_close(isin, e["day"])
        if d2:
            res["day2"] = (d2 - res["entry_px"]) / res["entry_px"] * 100
        rows.append({**e, "verdict": v.kind, "blocks": v.blocks, "reasons": v.reasons,
                     "score": v.score, **res})

    print(f"оценено: {len(rows)}\n")
    ok = [r for r in rows if r["eod"] is not None]

    print("=== ПО ВЕРДИКТАМ (к концу дня) ===")
    for kind, label in (("buy", "🟢 БРАТЬ"), ("maybe", "🟡 ОСТОРОЖНО"),
                        ("late", "🟠 ПОЗДНО"), ("no", "⛔ НЕ БРАТЬ")):
        xs = [r["eod"] for r in ok if r["verdict"] == kind]
        print(f"{label:14} {stat(xs)}")

    print("\n=== НЕ БРАТЬ: по причине отсева ===")
    groups: dict[str, list[float]] = {}
    for r in ok:
        if r["verdict"] != "no":
            continue
        key = r["blocks"][0].split("(")[0].split("—")[0].strip() if r["blocks"] else "низкий балл"
        groups.setdefault(key, []).append(r["eod"])
    for key, xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {key[:48]:50} {stat(xs)}")

    print("\n=== ПОПРАВКА НА РЫНОК (альфа = результат − движение самой бумаги за день) ===")
    for kind, label in (("buy", "🟢 БРАТЬ"), ("maybe", "🟡 ОСТОРОЖНО"),
                        ("late", "🟠 ПОЗДНО"), ("no", "⛔ НЕ БРАТЬ")):
        xs = [r["eod"] for r in ok if r["verdict"] == kind]
        moves = [r["day_move"] for r in ok if r["verdict"] == kind and r["day_move"] is not None]
        if not xs or not moves:
            continue
        alpha = [r["eod"] - r["day_move"] for r in ok
                 if r["verdict"] == kind and r["day_move"] is not None]
        print(f"{label:14} результат {statistics.median(xs):+6.2f}% | "
              f"бумага за день {statistics.median(moves):+6.2f}% | "
              f"альфа {statistics.median(alpha):+6.2f}%")

    print("\n=== РИСК: сколько ещё падало после входа (медиана) ===")
    for kind, label in (("buy", "🟢 БРАТЬ"), ("maybe", "🟡 ОСТОРОЖНО"),
                        ("late", "🟠 ПОЗДНО"), ("no", "⛔ НЕ БРАТЬ")):
        xs = [r["low_after"] for r in ok if r["verdict"] == kind]
        if xs:
            print(f"{label:14} медиана просадки {statistics.median(xs):+6.2f}% | "
                  f"худшая {min(xs):+6.2f}%")

    print("\n=== НА 2-й ДЕНЬ (план «держим 2 дня») ===")
    for kind, label in (("buy", "🟢 БРАТЬ"), ("maybe", "🟡 ОСТОРОЖНО"),
                        ("late", "🟠 ПОЗДНО"), ("no", "⛔ НЕ БРАТЬ")):
        xs = [r["day2"] for r in ok if r["verdict"] == kind and r.get("day2") is not None]
        print(f"{label:14} {stat(xs)}")

    print("\n=== ПРОСТОЙ БЕНЧМАРК: все падения без фильтров, к концу дня ===")
    print(f"  {stat([r['eod'] for r in ok])}")

    json.dump(rows, open("rejects_analysis.json", "w"), ensure_ascii=False, indent=1, default=str)
    print("\nподробно: rejects_analysis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
