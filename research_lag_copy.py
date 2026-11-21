"""«Купить позже» — плюс или нет: копирование сделок владельца с задержкой.

Для каждой покупки владельца берём цену бумаги через L торговых дней после его
сделки (1, 2, 3, 5, 10, 22) и считаем:
  * сырой результат до горизонта H (5 / 22 / 60 дней от входа);
  * альфу = результат минус то, что дала бы медианная бумага своей вселенной
    за тот же срок (иначе «плюс» — это просто растущий рынок);
  * дистанцию от его входа: сколько процентов мы теряем/выигрываем, покупая позже.
"""
from __future__ import annotations

import glob
import json
import statistics as st
from pathlib import Path

CACHE = Path("data/hist")
LAGS = (0, 1, 2, 3, 5, 10, 22)
HORIZONS = (5, 22, 60)


def load() -> tuple[dict, dict, list[str]]:
    matrix: dict[str, dict[str, float]] = {}
    for f in glob.glob(str(CACHE / "bond-*.json")):
        tk = Path(f).name.split("-")[1]
        for r in json.loads(Path(f).read_text()):
            if r.get("CLOSE") and r.get("TRADEDATE"):
                matrix.setdefault(r["TRADEDATE"], {})[tk] = r["CLOSE"]
    return matrix, {}, sorted(matrix)


def bench(matrix, days, N) -> dict[str, float]:
    out = {}
    for i, d in enumerate(days):
        j = i + N
        if j >= len(days):
            continue
        a, b = matrix[d], matrix[days[j]]
        vals = []
        for t, p in a.items():
            bt = b.get(t)
            if p and bt:
                vals.append((bt - p) / p * 100)
        if len(vals) >= 30:
            out[d] = st.median(vals)
    return out


def med(v):
    v = [x for x in v if x is not None]
    return round(st.median(v), 2) if v else None


def pos(v):
    v = [x for x in v if x is not None]
    return round(sum(1 for x in v if x > 0) / len(v) * 100) if v else None


def main() -> None:
    matrix, _, days = load()
    idx = {d: i for i, d in enumerate(days)}
    benches = {N: bench(matrix, days, N) for N in HORIZONS}
    tx = json.loads((CACHE / "portfolio_tx.json").read_text())
    buys = [t for t in tx if t.get("type") == 1 and t.get("assetType") == 2]

    print(f"{'задержка':>9} │ {'сырой +5д':>10} {'альфа +5д':>10} │ {'сырой +22д':>11} {'альфа +22д':>11} │ {'альфа +60д':>11}")
    print("─" * 78)
    rows = []
    for L in LAGS:
        cells = {N: {"raw": [], "alpha": []} for N in HORIZONS}
        gap = []
        for t in buys:
            day, tk = (t.get("date") or "")[:10], t.get("ticker")
            if day not in idx or tk not in matrix.get(day, {}):
                continue
            i = idx[day]
            j = i + L
            if j >= len(days):
                continue
            base = matrix[days[j]].get(tk)
            if not base:
                continue
            own = t.get("pricePercent") or matrix[day][tk]
            gap.append((base - own) / own * 100)
            for N in HORIZONS:
                k = j + N
                if k < len(days) and matrix[days[k]].get(tk) and days[j] in benches[N]:
                    raw = (matrix[days[k]][tk] - base) / base * 100
                    cells[N]["raw"].append(raw)
                    cells[N]["alpha"].append(raw - benches[N][days[j]])
        line = f"{L:>6} дн. │ {med(cells[5]['raw']):>+9.2f}% {med(cells[5]['alpha']):>+9.2f}% │" \
               f" {med(cells[22]['raw']):>+10.2f}% {med(cells[22]['alpha']):>+10.2f}% │ {med(cells[60]['alpha']):>+10.2f}%"
        print(line)
        rows.append({"lag": L, "gap_from_owner": med(gap),
                     **{f"raw{N}": med(cells[N]['raw']) for N in HORIZONS},
                     **{f"alpha{N}": med(cells[N]['alpha']) for N in HORIZONS},
                     **{f"share{N}": pos(cells[N]['raw']) for N in HORIZONS},
                     "n": len(cells[22]['raw'])})
    print("\nразница цены с его входом (медиана, %):")
    for r in rows:
        print(f"  задержка {r['lag']:>2} дн.: {r['gap_from_owner']:+.2f}%  "
              f"(в плюсе +22 дн.: {r['share22']}%, n={r['n']})")
    Path(CACHE / "lag_copy.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
