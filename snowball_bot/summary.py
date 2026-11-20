"""Дневная сводка: что бот нашёл, что из этого вышло и где он ошибся.

Зачем: сигналы приходят «в моменте», но понять, работает ли стратегия
(«купил провал → держишь 2 дня»), можно только по факту. Поэтому:

  1. каждый отправленный сигнал пишется в журнал `dip_journal` (state);
  2. вечером команда `summary` берёт сигналы за день и по 10-минутным свечам
     MOEX считает, что было бы, если бы вы купили:
        * по цене из сигнала (как есть),
        * лимитом на 1% ниже (как советует бот);
     далее — результат через 30 мин, 1 ч, 2 ч, к концу дня и на второй день;
  3. отдельно — «разбор ошибок»: где сигнал был, а цена после него падала дальше.

Запуск: python3 bot.py summary [--dry-run] [--days 1]
"""
from __future__ import annotations

import argparse
import html
import logging
from datetime import datetime, timedelta

from . import analytics, dipscanner, formatter, sizing
from .config import Config
from .notify import build_notifiers
from .store import open_store

log = logging.getLogger(__name__)

JOURNAL_KEY = "dip_journal"


# ---------------------------------------------------------------------------
# Журнал сигналов
# ---------------------------------------------------------------------------
def journal_add(store, dips: list[dict], cfg: Config, profiles: dict | None = None) -> None:
    """Записать отправленные сигналы в журнал (для вечернего разбора)."""
    journal = store.get_meta(JOURNAL_KEY, {}) or {}
    day = dipscanner.now_msk().strftime("%Y-%m-%d")
    entries = journal.get(day) or []
    known = {e.get("isin") for e in entries}
    for d in dips:
        isin = d.get("isin") or d["secid"]
        if isin in known:
            continue
        tm = d.get("timing") or {}
        prof = (profiles or {}).get(d.get("isin") or "") or {}
        entries.append({
            "isin": d.get("isin"),
            "secid": d["secid"],
            "name": d.get("name"),
            "time": tm.get("low_time") or dipscanner.now_msk().strftime("%H:%M"),
            "price_pct": d.get("price_pct"),
            "facevalue": d.get("facevalue") or 1000,
            "nkd": d.get("nkd") or 0,
            "drop": round(d.get("drop") or 0, 2),
            "dip_stats": prof.get("dip_stats"),
            "kind": (d.get("verdict") or {}).get("kind") if isinstance(d.get("verdict"), dict) else d.get("verdict_kind"),
        })
    journal[day] = entries[-60:]
    # храним 30 дней
    cutoff = (dipscanner.now_msk() - timedelta(days=30)).strftime("%Y-%m-%d")
    journal = {k: v for k, v in journal.items() if k >= cutoff}
    store.set_meta(JOURNAL_KEY, journal)


def journal_get(store, day: str) -> list[dict]:
    return (store.get_meta(JOURNAL_KEY, {}) or {}).get(day) or []


# ---------------------------------------------------------------------------
# Расчёт результата по свечам
# ---------------------------------------------------------------------------
def _intraday(isin: str, day: str) -> list[dict]:
    """10-минутные свечи за конкретный день (МСК)."""
    url = (f"{analytics.ISS}/engines/stock/markets/bonds/securities/{isin}/candles.json"
           f"?iss.meta=off&interval=10&from={day}&till={day}")
    rows = analytics._block(url, "candles")
    out = []
    for r in rows:
        if not r.get("close") or not r.get("begin"):
            continue
        try:
            dt = datetime.fromisoformat(str(r["begin"]).replace(" ", "T")[:19])
        except Exception:  # noqa: BLE001
            continue
        out.append({"dt": dt, "close": r["close"], "high": r.get("high"), "low": r.get("low")})
    return out


def evaluate(entry: dict, days_prices: dict[str, float] | None = None) -> dict | None:
    """Что было бы, если купить по сигналу. Возвращает проценты по горизонтам."""
    isin = entry.get("isin")
    if not isin:
        return None
    day = (entry.get("day") or "")
    candles = _intraday(isin, day)
    if len(candles) < 2:
        return None
    # цена входа: закрытие свечи, ближайшей к времени сигнала (или первая после)
    sig_time = entry.get("time") or "00:00"
    try:
        hh, mm = (int(x) for x in sig_time.split(":"))
    except ValueError:
        hh, mm = 0, 0
    sig_dt = candles[0]["dt"].replace(hour=hh, minute=mm, second=0, microsecond=0)
    after = [c for c in candles if c["dt"] >= sig_dt]
    if not after:
        return None
    entry_price = after[0]["close"]

    def ret_at(minutes: int) -> float | None:
        target = sig_dt + timedelta(minutes=minutes)
        window = [c for c in candles if c["dt"] <= target]
        if not window:
            return None
        return (window[-1]["close"] - entry_price) / entry_price * 100

    eod = (candles[-1]["close"] - entry_price) / entry_price * 100
    low_after = min(c["close"] for c in after)
    high_after = max(c["close"] for c in after)
    out = {
        "kind": entry.get("kind"),
        "entry_price_pct": entry_price,
        "r30": ret_at(30),
        "r60": ret_at(60),
        "r120": ret_at(120),
        "eod": eod,
        "low_after": (low_after - entry_price) / entry_price * 100,
        "high_after": (high_after - entry_price) / entry_price * 100,
        "limit_filled": low_after <= entry_price * 0.99,
        "limit_eod": (candles[-1]["close"] - entry_price * 0.99) / (entry_price * 0.99) * 100,
        "day2": (days_prices or {}).get("day2"),
    }
    return out


def day2_close(isin: str, day: str) -> float | None:
    """Закрытие следующего торгового дня (для плана «держим 2 дня»)."""
    rows = analytics.daily_candles(isin, days=10)
    closes = [r for r in rows if r.get("close") and str(r.get("begin") or r.get("end") or "")[:10] > day]
    if not closes:
        return None
    return closes[0]["close"]


# ---------------------------------------------------------------------------
# Сообщение
# ---------------------------------------------------------------------------
def _pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{digits}f}%".replace(".", ",")


def _money(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def build_summary(day: str, entries: list[dict], results: list[dict], cfg: Config,
                  breadth: dict | None = None) -> str:
    head = f"📊 <b>Сводка дня по провалам</b> — {datetime.fromisoformat(day):%d.%m.%Y}"
    if not entries:
        return (head + "\n\nСегодня сигналов не было — либо рынок спокоен, либо все "
                "падения отсеяны фильтрами.")

    parts = [head, f"<i>Сигналов: {len(entries)} · оценка по биржевым свечам</i>", ""]

    if breadth:
        parts.append(
            f"🌡 Рынок на момент скана: в минусе {breadth.get('down', 0)} из "
            f"{breadth.get('total', 0)} бумаг ({breadth.get('down_share', 0):.0f}%), "
            f"медиана {_pct(breadth.get('median'))}"
        )
        parts.append("")

    ok = [r for r in results if r]
    if ok:
        def avg(key: str) -> float | None:
            vals = [r[key] for r in ok if r.get(key) is not None]
            return sum(vals) / len(vals) if vals else None

        def share_pos(key: str) -> int:
            vals = [r[key] for r in ok if r.get(key) is not None]
            return round(sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else 0

        parts.append("📈 <b>Если бы покупали по сигналу</b>")
        for label, key in (("через 30 мин", "r30"), ("через 1 ч", "r60"),
                           ("через 2 ч", "r120"), ("к концу дня", "eod")):
            a = avg(key)
            if a is not None:
                parts.append(f"• {label}: <b>{_pct(a)}</b> (в плюсе {share_pos(key)}%)")
        d2 = [r["day2"] for r in ok if r.get("day2") is not None]
        if d2:
            parts.append(f"• держим до 2-го дня: <b>{_pct(sum(d2) / len(d2))}</b>")
        parts.append("")

        filled = [r for r in ok if r.get("limit_filled")]
        parts.append(
            f"🎯 <b>Лимит на 1% ниже</b>: исполнился бы в {len(filled)} из {len(ok)} случаев"
            + (f", средний результат к концу дня {_pct(sum(r['limit_eod'] for r in filled) / len(filled))}"
               if filled else "")
        )
        parts.append("")

        # в деньгах по вашему бюджету
        budget = cfg.my_capital * cfg.my_max_position_pct / 100
        with_eod = [r for r in ok if r.get("eod") is not None]
        best = max(with_eod, key=lambda r: r["eod"])
        worst = min(with_eod, key=lambda r: r["eod"])
        parts.append(f"💼 <b>На ваш капитал</b> ({_money(budget)} на идею):")
        parts.append(f"• лучшая идея дня: {_pct(best['eod'])} ≈ {_money(budget * best['eod'] / 100)}")
        parts.append(f"• худшая: {_pct(worst['eod'])} ≈ {_money(budget * worst['eod'] / 100)}")
        total = budget * (avg("eod") or 0) / 100 * len(with_eod)
        parts.append(f"• если бы взяли все {len(with_eod)} идей по {_money(budget)}: "
                     f"{_money(total)} за день")
        parts.append("")

    # разбор ошибок
    bad = [r for r in ok if (r.get("eod") or 0) < -2]
    if bad:
        parts.append("⚠️ <b>Где было бы больно</b> (цена падала дальше):")
        for r, e in [(r, ent) for r in bad for ent in entries if ent.get("isin") == r.get("isin")][:3]:
            parts.append(f"• {html.escape(str(e.get('name')))}: {_pct(r['eod'])} к концу дня, "
                         f"просадка после входа {_pct(r['low_after'])}")
        parts.append("")

    # проверка вердиктов: что стало с «брать» и с «не брать»
    if ok:
        by_kind = {}
        for r in ok:
            k = r.get("kind")
            if not k:
                continue
            by_kind.setdefault(k, []).append(r.get("eod") or 0)
        if by_kind:
            names = {"buy": "🟢 БРАТЬ", "maybe": "🟡 ОСТОРОЖНО",
                     "late": "🟠 ПОЗДНО", "no": "⛔ НЕ БРАТЬ"}
            parts.append("🧭 <b>Проверка вердиктов</b> (к концу дня):")
            for k in ("buy", "maybe", "late", "no"):
                if k in by_kind:
                    vals = by_kind[k]
                    up = round(sum(1 for v in vals if v > 0) / len(vals) * 100)
                    parts.append(f"• {names[k]}: {len(vals)} сигн., средний "
                                 f"{_pct(sum(vals) / len(vals))}, в плюсе {up}%")
            parts.append("")

    parts.append("📌 <i>Оценка приблизительная: без комиссии, спреда и налога; "
                 "цена входа — закрытие ближайшей 10-минутной свечи после сигнала.</i>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def run(cfg: Config, days: int = 1, dry_run: bool = False, date: str | None = None) -> int:
    store = open_store(cfg)
    notifiers = build_notifiers(cfg)
    # ВАЖНО: «сегодня» считаем по московскому времени (раньше брали UTC,
    # и после 21:00 МСК сводка искала сигналы в следующем дне — «сигналов не было»)
    today = dipscanner.now_msk().strftime("%Y-%m-%d")
    target_days = ([date] if date else
                   [(dipscanner.now_msk() - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(max(1, days))])

    all_entries, all_results = [], []
    for day in reversed(target_days):
        entries = journal_get(store, day)
        if not entries:
            continue
        prices = {}
        for e in entries:
            e["day"] = day
            d2 = day2_close(e.get("isin") or "", day)
            prices["day2"] = None
            if d2 and e.get("price_pct"):
                # результат «держим 2 дня» считаем от цены входа: см. evaluate
                prices["day2"] = None
            res = evaluate(e, prices)
            if res and d2:
                entry_px = res["entry_price_pct"]
                res["day2"] = (d2 - entry_px) / entry_px * 100
            if res:
                all_results.append(res)
                e["_res"] = res
        all_entries.extend(entries)

    breadth = store.get_meta("market_breadth", {}) or {}
    if not all_entries:
        # подсказка: когда сигналы были в последний раз (журнал хранит 30 дней)
        journal = store.get_meta(JOURNAL_KEY, {}) or {}
        last_day = max((d for d, v in journal.items() if v), default=None)
        store.close()
        if last_day:
            text = (f"📊 <b>Сводка дня</b> — {datetime.fromisoformat(today):%d.%m.%Y}\n\n"
                    f"Сегодня сигналов не было (журнал пуст). Последний день с сигналами — "
                    f"{datetime.fromisoformat(last_day):%d.%m.%Y}, там {len(journal[last_day])} шт.\n"
                    f"Собрать за него: <code>/summary {last_day}</code> или "
                    f"Actions → «Сводка дня» с days=2.")
        else:
            text = (f"📊 <b>Сводка дня</b> — {datetime.fromisoformat(today):%d.%m.%Y}\n\n"
                    "Журнал сигналов пока пуст: он наполняется, когда сканер реально "
                    "отправляет сигналы (или по команде /dips).")

    else:
        text = build_summary(today, all_entries, all_results, cfg, breadth)

    store.close()

    if dry_run:
        print(formatter.to_plain(text))
        return 0

    sent = False
    for n in notifiers:
        for chunk in formatter.split_for_limit(text, limit=3900):
            try:
                sent |= n.send(chunk, formatter.to_plain(chunk))
            except Exception as e:  # noqa: BLE001
                log.error("Отправка (%s): %s", n.name, e)
    log.info("Сводка отправлена: сигналов %d", len(all_entries))
    return 0 if sent else 1
