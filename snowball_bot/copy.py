"""Как копировать сделку владельца: лимитом по его цене и на его горизонте.

Измерено (COPY_LAG.md, 1989 покупок за 3 года):

  * вход по его цене даёт альфу +0,46% за 22 дня, вход по закрытию дня — +0,46%
    только если цена не ушла; в среднем рыночная покупка «по факту» режет
    преимущество вдвое (69% его сделок исполнены НИЖЕ закрытия дня);
  * задержка 1–3 дня почти ничего не стоит (+0,38% против +0,46%),
    5 дней — половина, 10 дней — четверть, 22 дня — ноль;
  * держать надо месяц: +5 дней +0,12%, +22 дня +0,38%, +60 дней +0,50%;
  * прибыльных сделок 52–69%, то есть каждая третья-четвёртая в минусе —
    это норма, а не сбой.

Модуль превращает это в строки подсказки в уведомлении о сделке.
"""
from __future__ import annotations

from datetime import datetime

# ---- измеренные константы (не менять без нового замера) -------------------
ALPHA_BY_LAG = {0: 0.46, 1: 0.38, 2: 0.38, 3: 0.36, 5: 0.29, 10: 0.20, 22: 0.08}
LAG_WARN_DAYS = 5          # после этого альфа съедена наполовину
LAG_STALE_DAYS = 10        # здесь остаётся четверть — почти нет смысла
HOLD_NOTE = ("горизонт месяц: +5 дн. даёт +0,12%, +22 дн. +0,38%, "
             "+60 дн. +0,50% — это позиция, а не дневная сделка")


def _money(v: float) -> str:
    """9895.5 → «9 895,50» — как принято в русских карточках бота."""
    return f"{v:,.2f}".replace(",", "\u00a0").replace(".", ",")


def _pct(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def _alpha_at(lag: int) -> float:
    keys = sorted(ALPHA_BY_LAG)
    if lag <= keys[0]:
        return ALPHA_BY_LAG[keys[0]]
    if lag >= keys[-1]:
        return ALPHA_BY_LAG[keys[-1]]
    lo = max(k for k in keys if k <= lag)
    return ALPHA_BY_LAG[lo]


def lag_days(tx: dict, now: datetime | None = None) -> int:
    """Сколько дней прошло с даты сделки (МСК)."""
    raw = (tx.get("date") or "")[:10]
    if not raw:
        return 0
    try:
        made = datetime.fromisoformat(raw).date()
    except Exception:  # noqa: BLE001
        return 0
    today = (now or datetime.now()).date()
    return max(0, (today - made).days)


def limit_price(tx: dict, info: dict | None = None) -> tuple[float | None, float | None]:
    """Его цена и наша лимитная: чуть ниже, чтобы не гнаться за рынком.

    Возвращает (его цена за штуку, наша лимитная цена). Если в сделке нет цены,
    берём текущую из карточки бумаги (info).
    """
    price = tx.get("price")
    nominal = tx.get("nominal") or (info or {}).get("nominal")
    if not price and info:
        pct = (info or {}).get("price") or (info or {}).get("price_pct_of_nominal")
        if pct and nominal:
            price = pct * nominal / 100 if pct < 200 else pct
    if not price:
        return None, None
    return float(price), float(price) * 0.998      # −0,2% от его цены


def lines(tx: dict, info: dict | None = None, now: datetime | None = None) -> list[str]:
    """Строки для уведомления: по какой цене брать и сколько ещё можно ждать."""
    if tx.get("type") != 1:          # только покупки: продажу копировать не надо
        return []
    lag = lag_days(tx, now)
    own, lim = limit_price(tx, info)
    out: list[str] = []
    if own and lim:
        lim_txt = _money(lim)
        own_txt = _money(own)
        out.append(f"🎯 <b>Брать лимитом {lim_txt} ₽</b> — не выше его цены {own_txt} ₽")
    alpha = _alpha_at(lag)
    if lag <= 3:
        out.append(f"⏱ Задержка {lag} дн. — не критично: остаётся "
                   f"~{_pct(alpha)}% из {_pct(ALPHA_BY_LAG[0])}% преимущества")
    elif lag < LAG_STALE_DAYS:
        out.append(f"⚠️ Задержка {lag} дн. — половина преимущества уже съедена "
                   f"(остаётся ~{_pct(alpha)}%)")
    else:
        out.append(f"⛔ Задержка {lag} дн. — преимущества почти не осталось "
                   f"(~{_pct(alpha)}%); входить только если бумага нравится сама по себе")
    out.append(f"📅 <i>{HOLD_NOTE}</i>")
    return out
