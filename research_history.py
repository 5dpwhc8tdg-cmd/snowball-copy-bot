"""Проверка правил сканера на прошлом: сборка событий «бумага упала» из дневной истории.

Событие = бумага, закрывшая день падением ≥ порога к предыдущему закрытию при
достаточной ликвидности. Дальше (в research_history.py) по 10-минутным свечам
находится момент внутри дня, когда сканер сработал бы, и измеряется отскок.
"""
from __future__ import annotations

import glob
import json
from datetime import date, datetime
from pathlib import Path

CACHE = Path("data/hist")

# фильтры сканера (по умолчанию)
MIN_TRADES = 5
MIN_VALUE = 30_000
MIN_PRICE_PCT = 70.0
DROP_THRESHOLD = 4.0


def load_days() -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for f in sorted(glob.glob(str(CACHE / "daily-*.json"))):
        day = f.split("daily-")[1][:10]
        out[day] = {r["SECID"]: r for r in json.loads(Path(f).read_text())}
    return dict(sorted(out.items()))


def _years_left(matdate: str | None) -> float | None:
    if not matdate:
        return None
    try:
        d = datetime.fromisoformat(matdate).date()
    except Exception:  # noqa: BLE001
        return None
    return (d - date.today()).days / 365.25


def build_events(days: dict[str, dict[str, dict]], min_drop: float = DROP_THRESHOLD) -> list[dict]:
    keys = list(days)
    events: list[dict] = []
    for i in range(1, len(keys)):
        day, prev_day = keys[i], keys[i - 1]
        prev = days[prev_day]
        # широта рынка на этот день (для проверки «паника на всём рынке»)
        moves = []
        for r in days[day].values():
            pc = (prev.get(r["SECID"]) or {}).get("CLOSE")
            if pc and r.get("CLOSE") and (r.get("NUMTRADES") or 0) >= 3:
                moves.append((r["CLOSE"] - pc) / pc * 100)
        down_share = (sum(1 for m in moves if m < 0) / len(moves) * 100) if moves else None

        for secid, r in days[day].items():
            pc = (prev.get(secid) or {}).get("CLOSE")
            cl = r.get("CLOSE")
            if not pc or not cl or pc <= 0:
                continue
            drop = (pc - cl) / pc * 100
            if drop < min_drop:
                continue
            nt = r.get("NUMTRADES") or 0
            val = r.get("VALUE") or 0
            face = r.get("FACEVALUE") or 0
            reasons = []
            if nt < MIN_TRADES:
                reasons.append(f"сделок {nt}")
            if val < MIN_VALUE:
                reasons.append(f"оборот {val:.0f} ₽")
            if cl < MIN_PRICE_PCT:
                reasons.append(f"цена {cl:.1f}% от номинала")
            yl = _years_left(r.get("MATDATE"))
            if yl is not None and yl < 0:
                reasons.append("срок погашения прошёл")
            if yl is not None and yl < 0.15:
                reasons.append("до погашения меньше 2 месяцев")
            events.append({
                "day": day,
                "secid": secid,
                "isin": None,
                "name": r.get("SHORTNAME"),
                "prev_close": pc,
                "close": cl,
                "drop_day_pct": drop,
                "numtrades": nt,
                "value": val,
                "low": r.get("LOW"),
                "high": r.get("HIGH"),
                "waprice": r.get("WAPRICE"),
                "yield": r.get("YIELDCLOSE"),
                "matdate": r.get("MATDATE"),
                "facevalue": face,
                "market_down_share": down_share,
                "reject_reasons": reasons,
                "clean": not reasons,
            })
    events.sort(key=lambda e: (e["day"], -e["drop_day_pct"]))
    return events


if __name__ == "__main__":
    days = load_days()
    ev = build_events(days)
    print(f"дней: {len(days)}, событий (падение ≥ {DROP_THRESHOLD}%): {len(ev)}")
    clean = [e for e in ev if e["clean"]]
    print(f"проходят фильтры сканера: {len(clean)}  отсеяно: {len(ev) - len(clean)}")
    by_day: dict[str, int] = {}
    for e in ev:
        by_day[e["day"]] = by_day.get(e["day"], 0) + 1
    print("по дням:", by_day)
    print(f"уникальных бумаг: {len(set(e['secid'] for e in ev))}")
    Path("data/hist/events.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1))
    for e in ev[:8]:
        print(f"  {e['day']} {e['secid']:14s} {e['name'][:26]:26s} {e['drop_day_pct']:5.1f}% "
              f"nt={e['numtrades']:4d} {'OK' if e['clean'] else '|'.join(e['reject_reasons'])}")


# ---------------------------------------------------------------------------
# Отскок по 10-минутным свечам: где сканер сработал бы и что было дальше
# ---------------------------------------------------------------------------
def scan_intraday(candles: list[dict], prev_close: float, min_drop: float = 4.0) -> dict | None:
    """Имитация логики сканера на свечах одного дня.

    Сканер сравнивает текущую цену с максимумом за последние 30 минут и с
    дневным максимумом. Здесь то же самое: на каждой свече считаем падение от
    максимума дня до этой свечи, срабатываем на первой, где оно ≥ порога.
    """
    if len(candles) < 3:
        return None
    peak = None
    for i, c in enumerate(candles):
        cl = c.get("close")
        if not cl:
            continue
        peak = cl if peak is None else max(peak, cl)
        drop = (peak - cl) / peak * 100 if peak else 0
        drop_prev = (prev_close - cl) / prev_close * 100 if prev_close else 0
        if drop >= min_drop or drop_prev >= min_drop:
            rest = [x for x in candles[i + 1:] if x.get("close")]
            nxt = rest[:12]                       # 2 часа
            after = rest
            def pct(vals, base):
                return (max(vals) - base) / base * 100 if vals and base else None
            eod = candles[-1].get("close")
            lows = [x.get("low") for x in after if x.get("low")]
            return {
                "trigger_i": i,
                "trigger_time": c.get("begin"),
                "entry": cl,
                "candles_total": len(candles),
                "drop_at_trigger": round(drop, 2),
                "max_2h_pct": round(pct([x["close"] for x in nxt], cl), 2) if nxt else None,
                "max_day_pct": round(pct([x["close"] for x in after], cl), 2) if after else None,
                "eod_pct": round((eod - cl) / cl * 100, 2) if eod else None,
                "min_after_pct": round((min(lows) - cl) / cl * 100, 2) if lows else None,
                "to_trigger_min": i * 10,
            }
    return None


def measure(events: list[dict], limit: int | None = None, refresh: bool = False) -> list[dict]:
    from history_fetch import fetch_candles

    out = []
    todo = [e for e in events if e["clean"]]
    if limit:
        todo = todo[:limit]
    for n, e in enumerate(todo, 1):
        cs = fetch_candles(e["secid"], e["day"], e["day"], 10, refresh=refresh)
        e = dict(e)
        e["candles"] = len(cs)
        res = scan_intraday(cs, e["prev_close"])
        if res:
            e.update(res)
            e["measured"] = True
        else:
            e["measured"] = False
            # если сканер не сработал бы — фиксируем день как «сигнала не было»
            e["why_no_trigger"] = "нет свечей" if not cs else "падение не дошло до порога на свечах"
        out.append(e)
        if n % 25 == 0:
            print(f"  … {n}/{len(todo)}")
    return out


if __name__ == "__main__" and len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "measure":
    days = load_days()
    events = json.loads(Path("data/hist/events.json").read_text())
    res = measure(events)
    Path("data/hist/measured.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    ok = [r for r in res if r.get("measured")]
    print(f"событий: {len(res)}, измерено по свечам: {len(ok)}")
