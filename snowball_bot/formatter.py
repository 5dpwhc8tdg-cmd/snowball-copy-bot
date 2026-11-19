"""Формирование текста уведомлений."""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from . import api

BUY = "buy"
SELL = "sell"
OTHER = "other"


def _fmt_money(value: float | None, currency: str = "RUB") -> str:
    if value is None:
        return "—"
    sign = "₽" if currency == "RUB" else currency
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + f" {sign}"


def _num(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def _fmt_qty(value: float | int | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    return f"{int(v)}" if v == int(v) else f"{v:,.2f}".replace(",", " ").replace(".", ",")


def classify(tx: dict) -> str:
    t = tx.get("type")
    if t == api.TX_BUY:
        return BUY
    if t == api.TX_SELL:
        return SELL
    return OTHER


def tx_datetime(tx: dict, tz: str) -> datetime:
    raw = tx.get("date") or tx.get("justDate") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.astimezone(ZoneInfo(tz))
    except Exception:
        return datetime.now(ZoneInfo(tz))


def asset_page_url(tx: dict) -> str:
    ticker = tx.get("ticker") or ""
    exchange = tx.get("exchange") or "MCX"
    currency = tx.get("currency") or "RUB"
    return f"https://snowball-income.com/public/asset/{ticker}.{exchange}.{currency}"



def analytics_lines(profile: dict | None, price_pct: float | None = None,
                    html_mode: bool = True) -> list[str]:
    """Компактная аналитика по бумаге: купон, ликвидность, история, личная статистика падений."""
    if not profile:
        return []
    esc = html.escape if html_mode else (lambda s: s)
    out: list[str] = []

    # 1) купон: сколько и когда
    cp_days = profile.get("next_coupon_days")
    cp_val = profile.get("next_coupon_value")
    cp_pct = profile.get("next_coupon_pct") or profile.get("coupon_pct")
    if cp_days is not None and cp_val:
        txt = f"💰 Купон {_num(cp_val, 2)} ₽/шт"
        if cp_pct:
            txt += f" ({_num(cp_pct, 1)}% годовых)"
        txt += f" через {cp_days} дн. ({profile.get('next_coupon_date')})"
        if cp_days <= 3:
            txt += " — ⚠️ цена уже включает НКД, покупать «под купон» смысла нет"
        out.append(txt)

    # 2) ликвидность
    avg = profile.get("avg_value_5d")
    if avg:
        mark = "жидкая" if avg >= 3_000_000 else "средняя" if avg >= 500_000 else "тонкая"
        out.append(f"📊 Оборот 5 дн.: {_fmt_money(avg)}/день — {mark}")

    # 3) история цены
    mx, mn, dd = profile.get("max_52w"), profile.get("min_52w"), profile.get("drawdown_from_max_pct")
    if mx and mn:
        txt = f"📈 52 нед.: {_num(mn, 1)}–{_num(mx, 1)}%"
        if dd is not None:
            txt += f", сейчас {_num(dd, 1)}% от максимума"
        out.append(txt)

    # 4) личная статистика падений — самое полезное для решения
    n = profile.get("prev_drops")
    reb = profile.get("prev_drops_rebound_median_pct")
    share = profile.get("prev_drops_up_share_pct")
    if n and reb is not None:
        verdict = ("отскакивает" if reb > 0.5 and (share or 0) >= 55
                   else "ведёт себя непредсказуемо" if reb > -0.5
                   else "⚠️ падения не выкупаются")
        out.append(f"🔁 Таких падений за полгода: {n}, через 2 дня медиана "
                   f"{reb:+.1f}%, в плюсе {share:.0f}% — {verdict}")

    # 5) сроки
    dtr = profile.get("days_to_redemption")
    if dtr:
        try:
            years = float(dtr) / 365
            if years < 3:
                out.append(f"⏳ До погашения {int(float(dtr))} дн. (~{years:.1f} г.)")
        except (TypeError, ValueError):
            pass
    offer = profile.get("offerdate") or profile.get("putoption")
    if offer and str(offer) not in ("None", "0000-00-00"):
        out.append(f"🚪 Оферта: {esc(str(offer))}")
    if profile.get("amort_upcoming"):
        out.append(f"🧩 Амортизация: {profile['amort_upcoming']} выплат тела впереди")
    return out


RATING_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
                "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"]


def rating_rank(r: str | None) -> int:
    import re as _re

    if not r:
        return len(RATING_ORDER)
    m = _re.match(r"^(?:RU|RUS)?(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)",
                  str(r).strip().upper().replace(" ", ""))
    if not m or m.group(1) not in RATING_ORDER:
        return len(RATING_ORDER)
    return RATING_ORDER.index(m.group(1))


def nqual_verdict(info: dict | None, moex: dict | None, rating: str | None,
                  kind: str = "buy") -> tuple[str, list[str]]:
    """Первый блок уведомления: можно ли это купить без статуса квала.

    Возвращает (заголовок, пояснения).
    Логика повторяет правила ЦБ, но это ОРИЕНТИР, а не гарантия:
    финальное слово — за приложением брокера в момент заявки.
    """
    notes: list[str] = []
    rating_ok = rating_rank(rating) <= RATING_ORDER.index("A+")
    qual_only = bool((moex or {}).get("is_qualified_only"))
    tech_default = bool((moex or {}).get("has_tech_default"))
    default = bool((moex or {}).get("has_default"))

    if kind == "sell":
        if rating_ok:
            head = (f"✅ <b>ПРОДАЖА</b> — рейтинг {html.escape(str(rating))} (A+ и выше): "
                    "бумага доступна неквалу и для покупки без теста")
        else:
            head = ("ℹ️ <b>ПРОДАЖА</b> — продать можно любую бумагу; "
                    "для ПОКУПКИ этой нужен тест неквала")
            notes.append("без теста неквал может купить максимум на 100 000 ₽ "
                         "(«право последнего слова»)")
        return head, notes
    if rating_ok:
        head = f"✅ <b>НЕКВАЛУ МОЖНО БЕЗ ТЕСТА</b> — рейтинг {html.escape(str(rating))} (A+ и выше)"
    elif qual_only and not rating_ok:
        head = "⛔ <b>ТОЛЬКО ДЛЯ КВАЛОВ</b> — неквалу недоступна даже после теста"
    elif rating:
        head = f"⚠️ <b>НУЖЕН ТЕСТ НЕКВАЛА</b> — рейтинг {html.escape(str(rating))} (ниже A+)"
    else:
        head = "⚠️ <b>НУЖЕН ТЕСТ НЕКВАЛА</b> — рейтинга A+ и выше нет"

    if moex:
        lvl = moex.get("list_level")
        if lvl:
            notes.append(f"уровень листинга {lvl} (3-й — вне котировальных списков)")
        if tech_default:
            notes.append("⚠️ на MOEX зафиксирован ТЕХНИЧЕСКИЙ ДЕФОЛТ")
        if default:
            notes.append("⛔ на MOEX зафиксирован ДЕФОЛТ")
        if qual_only:
            notes.append("MOEX: ISQUALIFIEDINVESTORS = 1 (бумага для квалов)")
        subtype = moex.get("bond_subtype")
        coupon = moex.get("coupon_details")
        if subtype and "оферт" in str(subtype).lower():
            notes.append(f"структура: {subtype}")
        if coupon:
            notes.append(f"купон: {coupon}")
    else:
        notes.append("данные MOEX недоступны — проверьте выпуск в приложении брокера")

    if not rating_ok:
        notes.append("без теста неквал может купить максимум на 100 000 ₽ "
                     "(«право последнего слова»)")
    notes.append("финально проверяйте в приложении БКС: «Инструменты → фильтр "
                 "«Для квалифицированных инвесторов»»")
    return head, notes


def warnings_for(tx: dict, info: dict | None) -> list[str]:
    """Подсказки новичку/неквалу: дешёвая бумага, отсутствие рейтинга и т.п."""
    notes: list[str] = []
    if not info:
        return notes
    pct = info.get("price_pct_of_nominal")
    if pct is not None:
        if pct < 75:
            notes.append(
                f"⚠️ Цена {_num(pct)}% от номинала — рынок оценивает выпуск как проблемный "
                f"(дефолт/реструктуризация/санация)."
            )
        elif pct > 135:
            notes.append(f"⚠️ Цена {_num(pct)}% от номинала — сильно выше номинала, риск большой просадки.")
    ytm = info.get("ytm")
    if ytm and ytm > 40:
        notes.append(f"⚠️ Доходность к погашению ≈ {_num(ytm, 0)}% — это плата за очень высокий риск.")
    return notes


def format_tx(
    tx: dict,
    tz: str,
    copy_scale: float = 0,
    info: dict | None = None,
    with_warnings: bool = True,
    for_telegram_html: bool = True,
    moex: dict | None = None,
    rating: str | None = None,
    show_verdict: bool = True,
    profile: dict | None = None,
    market: dict | None = None,
) -> str:
    """Одна сделка → человекочитаемый блок (HTML для Telegram)."""
    esc = html.escape if for_telegram_html else (lambda s: s)
    kind = classify(tx)
    icon = {"buy": "🟢", "sell": "🔴"}.get(kind, "⚪️")
    title = {"buy": "ПОКУПКА", "sell": "ПРОДАЖА"}.get(kind, "ОПЕРАЦИЯ")

    name = esc(tx.get("description") or tx.get("name") or "—")
    ticker = esc(tx.get("ticker") or "")
    qty = tx.get("amount")
    price = tx.get("price")
    total = tx.get("totalPrice")
    nkd = tx.get("nkd")
    commission = tx.get("commission")
    currency = tx.get("currency") or "RUB"
    nominal = tx.get("nominal")

    lines = [f"{icon} <b>{title}</b> — {name}"]
    sub = f"<code>{ticker}</code>"
    if nominal:
        sub += f" · номинал {_fmt_qty(nominal)}"
    lines.append(sub)

    verdict_notes: list[str] = []
    if show_verdict:
        head, verdict_notes = nqual_verdict(info, moex, rating, kind=kind)
        lines.append(head)

    price_line = f"{_fmt_qty(qty)} шт × {_fmt_money(price, currency)}"
    if nominal and price:
        price_line += f" ({_num(price / nominal * 100, 1)}% от номинала)"
    lines.append(price_line)
    if nkd:
        lines.append(f"НКД: +{_fmt_money(nkd, currency)} / шт")
    lines.append(f"<b>Итого: {_fmt_money(total, currency)}</b>")
    if commission:
        lines.append(f"Комиссия: {_fmt_money(commission, currency)}")

    if copy_scale and qty and copy_scale != 1:
        my_qty = max(0, round(qty * copy_scale))
        my_total = (total or 0) * (my_qty / qty if qty else 0)
        lines.append(
            f"↳ по вашему масштабу ×{copy_scale:g}: <b>{_fmt_qty(my_qty)} шт</b> "
            f"≈ {_fmt_money(my_total, currency)}"
        )

    dt = tx_datetime(tx, tz)
    stamp = dt.strftime("%d.%m.%Y") if (dt.hour == 0 and dt.minute == 0) else dt.strftime("%d.%m.%Y %H:%M")
    lines.append(f"🕒 {stamp} МСК")
    lines.append(f'🔗 <a href="{asset_page_url(tx)}">карточка бумаги</a>')

    # контекст сделки: где он купил относительно дня и что было с ценой потом
    if market:
        tr = market.get("price_in_day_range_pct")
        if tr is not None:
            where = ("у минимума дня" if tr < 25 else "в нижней половине дня" if tr < 55
                     else "в верхней половине дня" if tr < 80 else "у максимума дня")
            extra = f"· вход {where} ({_num(tr, 0)}% диапазона дня)"
            if market.get("eod_after_pct") is not None:
                extra += f", к концу дня {market['eod_after_pct']:+.1f}%"
            lines.append(extra)

    if profile:
        for ln in analytics_lines(profile, price_pct=(price / nominal * 100) if (price and nominal) else None):
            lines.append(ln)

    if show_verdict:
        for note in verdict_notes:
            lines.append(f"· {esc(note)}")

    if with_warnings:
        for w in warnings_for(tx, info):
            lines.append(esc(w))
    return "\n".join(lines)


def format_batch(
    txs: list[dict],
    tz: str,
    portfolio_title: str,
    portfolio_url: str,
    copy_scale: float = 0,
    infos: dict[str, dict] | None = None,
    with_warnings: bool = True,
    moex_map: dict[str, dict] | None = None,
    rating_map: dict[str, str] | None = None,
    profiles: dict[str, dict] | None = None,
    market_map: dict[str, dict] | None = None,
) -> str:
    infos = infos or {}
    moex_map = moex_map or {}
    rating_map = rating_map or {}
    profiles = profiles or {}
    market_map = market_map or {}
    buys = [t for t in txs if classify(t) == BUY]
    sells = [t for t in txs if classify(t) == SELL]
    buy_sum = sum((t.get("totalPrice") or 0) for t in buys)
    sell_sum = sum((t.get("totalPrice") or 0) for t in sells)

    header = f"📊 <b>{html.escape(portfolio_title)}</b> — новых сделок: {len(txs)}"
    summary = []
    if buys:
        summary.append(f"покупок {len(buys)} на {_fmt_money(buy_sum)}")
    if sells:
        summary.append(f"продаж {len(sells)} на {_fmt_money(sell_sum)}")
    if summary:
        header += "\n" + " · ".join(summary)

    blocks = [header, ""]
    for tx in txs:
        blocks.append(
            format_tx(
                tx,
                tz=tz,
                copy_scale=copy_scale,
                info=infos.get(tx.get("ticker") or ""),
                with_warnings=with_warnings,
                moex=moex_map.get(tx.get("ticker") or ""),
                rating=rating_map.get(tx.get("ticker") or ""),
                profile=profiles.get(tx.get("ticker") or ""),
                market=market_map.get(tx.get("ticker") or ""),
            )
        )
        blocks.append("➖➖➖➖➖➖➖➖")
    text = "\n".join(blocks).rstrip("➖➖➖➖➖➖➖➖\n")
    text += f'\n<a href="{portfolio_url}">Открыть портфель на Snowball</a>'
    return text


def split_for_limit(text: str, limit: int = 3900) -> list[str]:
    """Telegram режет сообщения на 4096 символах — бьём по блокам."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("➖➖➖➖➖➖➖➖"):
        candidate = (current + "➖➖➖➖➖➖➖➖" + block) if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


_TAG_RE = __import__("re").compile(r"<[^>]+>")


def to_plain(text_html: str) -> str:
    """Грубое превращение HTML-разметки Telegram в обычный текст (для MAX/консоли)."""
    import re as _re
    import html as _html

    text = _re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2: \1", text_html)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text)
