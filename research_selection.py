"""Выбор бумаг: альфа против медианы той же вселенной (честная база).

Прошлый расчёт сравнивал покупки владельца с индексом RGBI (гособлигации).
Но корпоративная бумага в среднем доходнее ОФЗ, поэтому такая «альфа» — это
в основном плата за кредитный риск, а не мастерство.

Здесь база другая: для каждой даты считаем, что дала бы **медианная бумага
из той же вселенной** (657 выпусков, которые владелец когда-либо покупал) за
тот же горизонт. Альфа = результат покупки минус эта медиана. Такой тест
отвечает на вопрос «выбирает ли он лучше среднего по своему полю».

Дополнительно: проверяем, переносится ли найденная закономерность на сигналы
сканера (падения ≥4%) — то есть годится ли она как фильтр.
"""
from __future__ import annotations

import collections
import glob
import json
import statistics as st
from pathlib import Path

CACHE = Path("data/hist")
HORIZONS = (5, 22, 60)


def load_matrix() -> dict[str, dict[str, float]]:
    """{дата: {тикер: закрытие}} по всем скачанным бумагам."""
    m: dict[str, dict[str, float]] = {}
    for f in glob.glob(str(CACHE / "bond-*.json")):
        ticker = Path(f).name.split("-")[1]
        for r in json.loads(Path(f).read_text()):
            if r.get("CLOSE") and r.get("TRADEDATE"):
                m.setdefault(r["TRADEDATE"], {})[ticker] = r["CLOSE"]
    return m


def row_index() -> dict[str, dict[str, dict]]:
    """{тикер: {дата: строка}} — чтобы брать доходность/дюрацию/купон."""
    out: dict[str, dict[str, dict]] = {}
    for f in glob.glob(str(CACHE / "bond-*.json")):
        ticker = Path(f).name.split("-")[1]
        out[ticker] = {r["TRADEDATE"]: r for r in json.loads(Path(f).read_text()) if r.get("TRADEDATE")}
    return out


def benchmarks(matrix: dict[str, dict[str, float]], days: list[str]) -> dict[int, dict[str, float]]:
    """{горизонт: {дата: медианный прирост этой вселенной за горизонт, %}}."""
    pos = {d: i for i, d in enumerate(days)}
    bench: dict[int, dict[str, float]] = {N: {} for N in HORIZONS}
    for i, d in enumerate(days):
        for N in HORIZONS:
            j = i + N
            if j >= len(days):
                continue
            vals = []
            today, later = matrix[d], matrix[days[j]]
            for tk, px in today.items():
                px2 = later.get(tk)
                if px and px2:
                    vals.append((px2 - px) / px * 100)
            if len(vals) >= 30:
                bench[N][d] = st.median(vals)
    return bench


def med(v):
    v = [x for x in v if x is not None]
    return st.median(v) if v else None


def share_pos(v):
    v = [x for x in v if x is not None]
    return sum(1 for x in v if x > 0) / len(v) * 100 if v else None


def fmt(x, sign=True):
    return "—" if x is None else (f"{x:+.2f}%" if sign else f"{x:.1f}%")


def main() -> None:
    matrix = load_matrix()
    series = row_index()
    days = sorted(matrix)
    bench = benchmarks(matrix, days)
    pos = {d: i for i, d in enumerate(days)}
    tx = json.loads((CACHE / "portfolio_tx.json").read_text())

    rows = []
    for t in tx:
        if t.get("type") != 1 or t.get("assetType") != 2:
            continue
        day, tk = (t.get("date") or "")[:10], t.get("ticker")
        if day not in pos or tk not in series or day not in series[tk]:
            continue
        r0 = series[tk][day]
        entry = t.get("pricePercent") or r0.get("CLOSE")
        rec = {
            "day": day, "ticker": tk, "name": t.get("description"), "entry": entry,
            "yield": r0.get("YIELDCLOSE"), "duration_days": r0.get("DURATION"),
            "coupon": r0.get("COUPONPERCENT"), "value": r0.get("VALUE"),
            "numtrades": r0.get("NUMTRADES"), "matdate": r0.get("MATDATE"),
        }
        for back in (5, 20):
            i = pos[day] - back
            if i >= 0:
                p = series[tk].get(days[i])
                if p and p.get("CLOSE"):
                    rec[f"mom{back}"] = (r0["CLOSE"] - p["CLOSE"]) / p["CLOSE"] * 100
        for N in HORIZONS:
            j = pos[day] + N
            if j < len(days):
                after = series[tk].get(days[j])
                if after and after.get("CLOSE") and day in bench[N]:
                    rec[f"r{N}"] = (after["CLOSE"] - entry) / entry * 100
                    rec[f"a{N}"] = rec[f"r{N}"] - bench[N][day]
        rows.append(rec)

    Path(CACHE / "owner_selection.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"покупок с полными данными: {len(rows)}\n")
    print("=== Альфа к медиане своей вселенной (не к RGBI) ===")
    for N in HORIZONS:
        v = [r.get(f"a{N}") for r in rows]
        print(f"  +{N:3d} дн.: медиана {fmt(med(v))}  в плюсе {fmt(share_pos(v), False)}  n={len([x for x in v if x is not None])}")

    def cut(name, key, bins, N=22):
        print(f"\n=== {name} (альфа +{N} дн. к вселенной) ===")
        for lo, hi, label in bins:
            v = [r.get(f"a{N}") for r in rows if r.get(key) is not None and lo <= r[key] < hi]
            n = len([x for x in v if x is not None])
            if n >= 20:
                print(f"  {label:24s} n={n:4d}  альфа {fmt(med(v))}  в плюсе {fmt(share_pos(v), False)}")

    cut("доходность к погашению", "yield",
        [(0, 14, "ниже 14%"), (14, 18, "14–18%"), (18, 25, "18–25%"), (25, 1000, "выше 25%")])
    cut("дюрация", "duration_days",
        [(0, 365, "до 1 года"), (365, 730, "1–2 года"), (730, 1460, "2–4 года"), (1460, 99999, "больше 4 лет")])
    cut("купон", "coupon", [(0, 12, "ниже 12%"), (12, 18, "12–18%"), (18, 100, "выше 18%")])
    cut("оборот в день покупки", "value",
        [(0, 3e5, "до 300 тыс"), (3e5, 3e6, "300 тыс–3 млн"), (3e6, 1e12, "больше 3 млн")])


if __name__ == "__main__":
    main()
