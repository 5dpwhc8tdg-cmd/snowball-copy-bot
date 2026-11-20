"""Вердикт по бумаге: покупать провал или нет — и одной строкой почему.

Раньше в сигнале был просто список фактов, и решение приходилось принимать
самому. Здесь факты взвешиваются в баллы, и получается один из четырёх выводов:

  🟢 БРАТЬ        — паника на нормальной бумаге, есть чем торговать и куда расти
  🟡 ОСТОРОЖНО    — половина «за», половина «против»: только мелким лотом
  🟠 ПОЗДНО       — провал был давно: отскока осталось мало, но сделка не отменяется —
                    уменьшаем размер и ставим лимит ниже рынка (см. rebound.py)
  ⛔ НЕ БРАТЬ     — дефолт/мусор/нет ликвидности/бумага в свободном падении

Баллы (плюс — за покупку, минус — против):
  +2  её собственные падения раньше отскакивали (медиана ≥ +1%, ≥60% случаев в плюс)
  +1  отскакивали вяло (медиана 0…+1%)
  −2  её падения НЕ выкупаются (медиана < 0)
  +1  рынок в целом спокоен → падение индивидуальное, а не «всё падает»
  −1  общая паника по рынку (≥65% бумаг в минусе)
  +1  ликвидность: оборот ≥ 1 млн ₽/день и спред ≤ 3%
  −1  тонкая: оборот < 300 тыс. ₽/день или спред > 5%
  +1  падение свежее (≤ 30 мин назад) — успеваете войти
      (за давность больше НЕ штрафуем: измерено, что через час остаётся
       ~+1,4% медианно и 76% случаев в плюсе — см. rebound.py)
  +1  цена в нижней половине дневного диапазона
  −3  бумага в глубоком даунтренде: −25% и больше от годового максимума
  −2  цена ниже 70% от номинала (рынок уже оценил как проблемную)
  ⛔  дефолт на MOEX, цена > 110% от номинала, валютный выпуск, нет данных
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import rebound

BUY, MAYBE, LATE, NO = "buy", "maybe", "late", "no"

ICON = {BUY: "🟢", MAYBE: "🟡", LATE: "🟠", NO: "⛔"}
LABEL = {BUY: "БРАТЬ", MAYBE: "ОСТОРОЖНО", LATE: "ПОЗДНО", NO: "НЕ БРАТЬ"}


@dataclass
class Verdict:
    kind: str
    score: int
    reasons: list[str] = field(default_factory=list)   # коротко, по делу
    blocks: list[str] = field(default_factory=list)    # почему нельзя брать
    estimate: dict = field(default_factory=dict)       # ожидаемый остаток отскока
    size_pct: int = 100                                # какую долю обычного размера брать

    @property
    def icon(self) -> str:
        return ICON[self.kind]

    @property
    def label(self) -> str:
        return LABEL[self.kind]

    def line(self) -> str:
        return f"{self.icon} <b>{self.label}</b> · {self.score:+d} балл(ов)"

    def short(self, limit: int = 2) -> str:
        return "; ".join(self.blocks or self.reasons[:limit])


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def decide(d: dict, profile: dict | None = None, breadth: dict | None = None,
           cfg=None) -> Verdict:
    """Вердикт по одному кандидату из сканера падений."""
    profile = profile or {}
    blocks: list[str] = []
    score = 0
    reasons: list[str] = []

    price_pct = _num(d.get("price_pct"))
    drop = _num(d.get("drop"))
    turnover = _num(d.get("valtoday"))
    bid, offer = d.get("bid"), d.get("offer")
    spread = ((offer - bid) / bid * 100) if (bid and offer and bid > 0) else None
    timing = d.get("timing") or {}
    since_low = timing.get("minutes_since_low")
    day_low, day_high = d.get("low"), d.get("high")

    # --- жёсткие запреты -------------------------------------------------
    flags = d.get("flags") or {}
    if flags.get("has_default"):
        blocks.append("на MOEX зафиксирован дефолт")
    if flags.get("has_tech_default"):
        blocks.append("технический дефолт")
    faceunit = (d.get("faceunit") or "SUR").upper()
    if faceunit not in ("SUR", "RUB"):
        blocks.append(f"валютный выпуск (номинал в {faceunit}) — валютный риск и налоги")
    if price_pct > 110:
        blocks.append(f"цена {price_pct:.0f}% от номинала — падение на большой премии, "
                      f"риск сдувания к номиналу")
    if price_pct and price_pct < 70:
        blocks.append(f"цена {price_pct:.0f}% от номинала — рынок уже считает выпуск проблемным")
    if not profile:
        blocks.append("нет данных по истории — оценить отскоки нечем")

    # --- личная статистика отскоков -------------------------------------
    med = profile.get("prev_drops_rebound_median_pct")
    up = profile.get("prev_drops_up_share_pct")
    n = profile.get("prev_drops")
    if med is not None:
        if med >= 1.0 and (up or 0) >= 60:
            score += 2
            reasons.append(f"выкупается: медиана +{med:.1f}%, в плюсе {up:.0f}%")
        elif med > 0:
            score += 1
            reasons.append(f"отскок слабый: медиана +{med:.1f}%")
        else:
            score -= 2
            reasons.append(f"не выкупается: медиана {med:+.1f}%")
    elif n == 0:
        reasons.append("таких падений у неё раньше не было")
        score -= 1

    # --- рынок -----------------------------------------------------------
    share = _num((breadth or {}).get("down_share"), -1)
    if share >= 65:
        score -= 1
        reasons.append(f"общая паника: в минусе {share:.0f}% рынка")
    elif 0 <= share < 40:
        score += 1
        reasons.append("рынок спокоен — падение своё")

    # --- ликвидность -----------------------------------------------------
    avg_val = _num(profile.get("avg_value_5d"))
    if (spread is not None and spread <= 3) and (avg_val >= 1_000_000 or turnover >= 1_000_000):
        score += 1
        reasons.append("ликвидная: узкий спред")
    elif (spread is not None and spread > 5) or (0 < avg_val < 300_000) or turnover < 100_000:
        score -= 1
        reasons.append("тонкая: широкий спред / малый оборот")

    # --- свежесть провала (мягкая поправка, без «отрицания») ---------------
    est = rebound.estimate(since_low, drop)
    if since_low is not None:
        if since_low <= 30:
            score += 1
            reasons.append(f"провал свежий: {since_low} мин")
        elif not est["useful"]:
            score -= 1
            reasons.append(f"отскока осталось мало: ~+{est['expect_pct']:.1f}%, "
                           f"{since_low} мин после провала")
        else:
            reasons.append(f"поздновато ({since_low} мин), но остаток "
                           f"+{est['expect_pct']:.1f}% ещё есть")

    # --- где цена в дневном диапазоне ------------------------------------
    if day_low and day_high and day_high > day_low:
        pos = (price_pct - day_low) / (day_high - day_low)
        if pos <= 0.35:
            score += 1
            reasons.append("цена у минимума дня")

    # --- тренд -----------------------------------------------------------
    dd = profile.get("drawdown_from_max_pct")
    if dd is not None and dd <= -25:
        score -= 3
        reasons.append(f"глубокий даунтренд: {dd:.0f}% от максимума")

    # --- решение ---------------------------------------------------------
    size = int(rebound.size_factor(since_low, drop) * 100)
    if blocks:
        return Verdict(NO, score, reasons, blocks, est, 0)
    if score >= 4:
        kind = BUY
    elif score >= 1:
        kind = MAYBE
    elif not est["useful"]:
        kind = LATE
    else:
        kind = NO
    return Verdict(kind, score, reasons, blocks, est, size)
