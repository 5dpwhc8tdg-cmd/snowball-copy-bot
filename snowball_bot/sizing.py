"""Пересчёт размера сделки под ваш капитал.

Проблема, которую решает модуль: у владельца портфеля ~7 млн ₽, у вас 100 тыс.
Простое масштабирование «×0,014» даёт 260 ₽ на сделку — меньше одного лота
(облигация стоит ~950–1000 ₽). Поэтому считаем так:

  1. масштаб = ваш капитал / капитал владельца (по умолчанию 100к / 7 млн ≈ ÷70);
  2. желаемый размер = его сделка × масштаб;
  3. но не меньше 1 лота (иначе сделку физически не повторить);
  4. и не больше доли капитала MAX_POSITION_PCT (по умолчанию 10%) — чтобы одна
     идея не съела весь счёт;
  5. в сообщение пишем: сколько штук, сколько это в рублях и % капитала,
     и сколько идей влезает в капитал при таком размере.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MySize:
    lots: int                 # сколько штук (лот облигации = 1 бумага)
    cost: float               # сколько это стоит с НКД, ₽
    cost_clean: float         # без НКД
    nkd_total: float
    pct_of_capital: float     # % капитала
    limited_by: str           # "scale" | "min_lot" | "max_position"
    fits_ideas: int           # сколько таких идей влезает в капитал
    note: str = ""


def compute(
    his_qty: float,
    price_per_unit: float,
    nkd_per_unit: float = 0.0,
    my_capital: float = 0.0,
    his_capital: float = 0.0,
    max_position_pct: float = 10.0,
) -> MySize | None:
    """Возвращает размер покупки под ваш капитал (или None, если данных мало)."""
    if not price_per_unit or price_per_unit <= 0:
        return None
    unit_cost = price_per_unit + (nkd_per_unit or 0)

    scale = (my_capital / his_capital) if (my_capital and his_capital) else 1.0
    desired_qty = (his_qty or 0) * scale
    desired_cost = desired_qty * unit_cost

    max_cost = my_capital * max_position_pct / 100 if my_capital else 0
    if max_cost and desired_cost > max_cost:
        lots = int(max_cost // unit_cost)
        limited_by = "max_position"
    else:
        lots = int(round(desired_qty))
        limited_by = "scale"
        if lots < 1:
            lots = 1
            limited_by = "min_lot"

    cost = lots * unit_cost
    note = ""
    if limited_by == "min_lot":
        note = ("его сделка в вашем масштабе меньше одного лота — берём минимум 1 шт")
    elif limited_by == "max_position":
        note = (f"ограничено долей капитала {max_position_pct:g}% "
                f"(иначе по масштабу вышло бы {desired_qty:.1f} шт)")
    fits = int(my_capital // cost) if (my_capital and cost) else 0
    return MySize(
        lots=lots,
        cost=cost,
        cost_clean=lots * price_per_unit,
        nkd_total=lots * (nkd_per_unit or 0),
        pct_of_capital=(cost / my_capital * 100) if my_capital else 0.0,
        limited_by=limited_by,
        fits_ideas=fits,
        note=note,
    )


def capital_split(my_capital: float, max_position_pct: float = 10.0,
                  reserve_pct: float = 20.0) -> tuple[float, float, int]:
    """Сколько держать в резерве и сколько идей брать одновременно.

    Возвращает (резерв ₽, деньги на идеи ₽, макс. число идей).
    """
    reserve = my_capital * reserve_pct / 100
    invest = my_capital - reserve
    max_ideas = int(invest // (my_capital * max_position_pct / 100)) if my_capital else 0
    return reserve, invest, max(1, max_ideas)
