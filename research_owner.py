"""Что покупает владелец портфеля: признаки его покупок и их альфа к индексу RGBI.

Выборка: ВСЕ покупки облигаций из публичного портфеля за июнь 2023 – ноябрь 2026
(~2000 сделок). Для каждой покупки берутся признаки бумаги **на дату покупки**
из дневной истории MOEX: цена от номинала, доходность к погашению, дюрация
(в днях), купон, оборот и число сделок, а также динамика за 5 и 20 дней до входа.
Результат считается на горизонтах 5 / 22 / 60 / 120 торговых дней как альфа
к индексу RGBI (индекс гособлигаций MOEX) — то есть «лучше ли рынка облигаций».

Запуск:  python3 research_owner.py            # отчёт
         python3 research_owner.py --csv      # выгрузить строки в CSV
"""
from __future__ import annotations

import collections
import csv
import json
import statistics as st
from pathlib import Path

from history_fetch import fetch_bond_history, fetch_index

TX = Path("data/hist/portfolio_tx.json")
FRM, TILL = "2023-06-01", "2023-11-30"  # заменяется ниже
STEP = 5  # как часто фиксируем «цены портфеля» для сравнения с рынком


def _pct(a, b):
    return (a - b) / b * 100 if b else None


def load_index() -> dict[str, float]:
    rows = fetch_index("RGBI", "2023-06-01", "2026-11-21")
    return {r["TRADEDATE"]: r["CLOSE"] for r in rows if r.get("CLOSE")}


def bond_series(ticker: str) -> dict[str, dict]:
    rows = fetch_bond_history(ticker, "2023-06-01", "2026-11-21")
    return {r["TRADEDATE"]: r for r in rows if r.get("CLOSE")}


def build() -> list[dict]:
    tx = json.loads(TX.read_text())
    buys = [t for t in tx if t.get("type") == 1 and t.get("assetType") == 2]
    idx = load_index()
    days = sorted(idx)
    pos_of = {d: i for i, d in enumerate(days)}
    cache: dict[str, dict[str, dict]] = {}
    out: list[dict] = []
    for n, t in enumerate(buys, 1):
        day, tk = (t.get("date") or "")[:10], t.get("ticker")
        if day not in pos_of or not tk:
            continue
        if tk not in cache:
            cache[tk] = bond_series(tk)
        series = cache[tk]
        row = series.get(day)
        if not row:
            continue
        i = pos_of[day]
        entry = t.get("pricePercent") or row.get("CLOSE")
        rec = {
            "day": day, "ticker": tk, "name": t.get("description"), "entry": entry,
            "yield": row.get("YIELDCLOSE"),
            "duration_days": row.get("DURATION"),
            "coupon": row.get("COUPONPERCENT"),
            "value": row.get("VALUE"),
            "numtrades": row.get("NUMTRADES"),
            "matdate": row.get("MATDATE"),
            "faceunit": row.get("FACEUNIT"),
        }
        # динамика бумаги до покупки
        for back in (5, 20):
            if i - back >= 0:
                prev_day = days[i - back]
                prow = series.get(prev_day)
                if prow and prow.get("CLOSE"):
                    rec[f"mom{back}"] = _pct(row["CLOSE"], prow["CLOSE"])
        # результат и альфа к RGBI
        for N in (5, 22, 60, 120):
            j = i + N
            if j < len(days):
                after = series.get(days[j])
                if after and after.get("CLOSE"):
                    r = _pct(after["CLOSE"], entry)
                    b = _pct(idx[days[j]], idx[day])
                    rec[f"r{N}"] = r
                    rec[f"a{N}"] = (r - b) if (r is not None and b is not None) else None
        out.append(rec)
        if n % 200 == 0:
            print(f"  обработано {n}/{len(buys)}", flush=True)
    return out


def med(v):
    v = [x for x in v if x is not None]
    return st.median(v) if v else None


def share_pos(v):
    v = [x for x in v if x is not None]
    return sum(1 for x in v if x > 0) / len(v) * 100 if v else None


def fmt(x, sign=True):
    return "—" if x is None else (f"{x:+.2f}%" if sign else f"{x:.2f}%")


def report(rows: list[dict]) -> None:
    print(f"\nпокупок облигаций с полными данными: {len(rows)}")
    print("\n=== Альфа к RGBI по горизонтам ===")
    for N in (5, 22, 60, 120):
        v = [r.get(f"a{N}") for r in rows]
        n = len([x for x in v if x is not None])
        print(f"  +{N:3d} дн.: медиана {fmt(med(v))}  в плюсе {fmt(share_pos(v), False)}  n={n}")
    print("\n=== Сырой результат (без поправки на индекс) ===")
    for N in (5, 22, 60, 120):
        v = [r.get(f"r{N}") for r in rows]
        print(f"  +{N:3d} дн.: медиана {fmt(med(v))}  в плюсе {fmt(share_pos(v), False)}")

    def cut(name, key, bins, horizon=22):
        print(f"\n=== {name} (альфа +{horizon} дн.) ===")
        for lo, hi, label in bins:
            v = [r.get(f"a{horizon}") for r in rows
                 if r.get(key) is not None and lo <= r[key] < hi]
            n = len([x for x in v if x is not None])
            if n >= 10:
                print(f"  {label:24s} n={n:3d}  альфа {fmt(med(v))}  в плюсе {fmt(share_pos(v), False)}")

    cut("доходность к погашению при покупке", "yield",
        [(0, 14, "ниже 14%"), (14, 18, "14–18%"), (18, 25, "18–25%"), (25, 1000, "выше 25%")])
    cut("дюрация", "duration_days",
        [(0, 365, "до 1 года"), (365, 730, "1–2 года"), (730, 1460, "2–4 года"), (1460, 99999, "больше 4 лет")])
    cut("купон", "coupon",
        [(0, 12, "ниже 12%"), (12, 18, "12–18%"), (18, 100, "выше 18%")])
    cut("оборот в день покупки", "value",
        [(0, 3e5, "до 300 тыс"), (3e5, 3e6, "300 тыс–3 млн"), (3e6, 1e12, "больше 3 млн")])
    cut("динамика за 5 дней до покупки", "mom5",
        [(-99, -2, "падала >2%"), (-2, 1, "ровно"), (1, 99, "росла >1%")])
    cut("динамика за 20 дней до покупки", "mom20",
        [(-99, -3, "падала >3%"), (-3, 1, "ровно"), (1, 99, "росла >1%")])
    cut("валюта номинала", "faceunit", [])if False else None
    print("\n=== Валюта номинала (альфа +22 дн.) ===")
    g = collections.defaultdict(list)
    for r in rows:
        g[r.get("faceunit") or "?"].append(r.get("a22"))
    for k, v in sorted(g.items(), key=lambda kv: -len(kv[1])):
        if len([x for x in v if x is not None]) >= 10:
            print(f"  {k:8s} n={len(v):3d}  альфа {fmt(med(v))}  в плюсе {fmt(share_pos(v), False)}")


if __name__ == "__main__":
    rows = build()
    Path("data/hist/owner_buys_3y.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    if "--csv" in __import__("sys").argv:
        keys = sorted({k for r in rows for k in r})
        with open("data/hist/owner_buys_3y.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print("CSV: data/hist/owner_buys_3y.csv")
    report(rows)
