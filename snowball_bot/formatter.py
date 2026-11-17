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
    if info.get("rating") is None:
        notes.append("ℹ️ Кредитного рейтинга нет → у брокера потребуется тест неквала.")
    return notes


def format_tx(
    tx: dict,
    tz: str,
    copy_scale: float = 0,
    info: dict | None = None,
    with_warnings: bool = True,
    for_telegram_html: bool = True,
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
) -> str:
    infos = infos or {}
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
