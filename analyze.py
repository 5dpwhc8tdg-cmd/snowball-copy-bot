"""Анализ активности публичного портфеля: сколько сделок, по каким бумагам,
насколько они «копируемые». Запуск:  python3 analyze.py [--days 30]
"""
from __future__ import annotations

import argparse
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from snowball_bot import api
from snowball_bot.config import Config, load_dotenv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pages", type=int, default=5)
    args = ap.parse_args()

    load_dotenv()
    cfg = Config()
    rows = api.fetch_all_transactions(cfg.portfolio_id, max_pages=args.pages, page_size=100)
    if not rows:
        print("Сделок не найдено")
        return 1

    edge = datetime.now(timezone.utc) - timedelta(days=args.days)
    in_window = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= edge:
            in_window.append(r)

    buys = [r for r in in_window if r["type"] == api.TX_BUY]
    sells = [r for r in in_window if r["type"] == api.TX_SELL]
    amounts = [r.get("totalPrice") or 0 for r in in_window]

    print(f"Портфель: {cfg.portfolio_title} ({cfg.portfolio_id})")
    print(f"Всего сделок в базе сервиса:  {len(rows)} (загружено {len(rows)})")
    print(f"Загружено страниц: {args.pages} × 100")
    if rows:
        print(f"Диапазон выгрузки: {rows[-1]['date'][:10]} … {rows[0]['date'][:10]}")
    print()
    print(f"Сделок за последние {args.days} дн.: {len(in_window)}")
    print(f"  покупок: {len(buys)} на {sum(r.get('totalPrice') or 0 for r in buys):,.0f} ₽".replace(",", " "))
    print(f"  продаж:  {len(sells)} на {sum(r.get('totalPrice') or 0 for r in sells):,.0f} ₽".replace(",", " "))
    if amounts:
        print(f"  медианная сделка: {statistics.median(amounts):,.0f} ₽".replace(",", " "))
        print(f"  средняя сделка:   {statistics.mean(amounts):,.0f} ₽".replace(",", " "))
    days = Counter(r["date"][:10] for r in in_window)
    if days:
        print(f"  сделок в день (в среднем): {len(in_window) / max(1, len(days)):.1f}")
    print()

    by_ticker = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "n_buy": 0, "n_sell": 0, "name": ""})
    for r in in_window:
        t = by_ticker[r["ticker"]]
        t["name"] = r.get("description") or ""
        if r["type"] == api.TX_BUY:
            t["buy"] += r.get("totalPrice") or 0
            t["n_buy"] += 1
        elif r["type"] == api.TX_SELL:
            t["sell"] += r.get("totalPrice") or 0
            t["n_sell"] += 1

    print(f"Уникальных бумаг за период: {len(by_ticker)}")
    print("\nТоп-15 по обороту:")
    for ticker, v in sorted(by_ticker.items(), key=lambda kv: -(kv[1]["buy"] + kv[1]["sell"]))[:15]:
        print(
            f"  {v['name'][:34]:36} {ticker:14} "
            f"куп {v['n_buy']:>3} / {v['buy']:>11,.0f} ₽   "
            f"прод {v['n_sell']:>3} / {v['sell']:>11,.0f} ₽".replace(",", " ")
        )

    # сколько бумаг «крутится» туда-обратно
    rotating = [t for t, v in by_ticker.items() if v["n_buy"] and v["n_sell"]]
    print(f"\nБумаг, которые и покупались, и продавались: {len(rotating)} из {len(by_ticker)}")
    print("(то есть это активная торговля, а не «купил и держи»)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
