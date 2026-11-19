"""Команда `dips`: сканер паник на рынке облигаций MOEX.

Отличия от отслеживания сделок владельца портфеля:
  * сигнал приходит по самому рынку — без задержки на публикацию сделки;
  * сразу видно, «плохая» это бумага или просто шум (фильтры);
  * в сообщении есть подсказка по входу/выходу и статистика отскоков.

Подключение в CI (GitHub Actions): отдельный workflow, шаг
  python3 bot.py dips --once
Отправка тем же каналом (Telegram/MAX/консоль).
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
import html
import logging

from . import analytics, dipscanner, formatter, ratings as ratings_api, sizing
from .config import Config
from .notify import build_notifiers
from .store import open_store

log = logging.getLogger(__name__)


def _num(value, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _fmt_money(v: float | None) -> str:
    if not v:
        return "—"
    return f"{v:,.0f} ₽".replace(",", " ")


SESSION_END = os.environ.get("DIP_SESSION_END", "23:50")   # конец торгов МСК (включая вечернюю)
MAIN_SESSION_END = os.environ.get("DIP_MAIN_SESSION_END", "18:39")


def _minutes_to_close(now: datetime | None = None) -> int | None:
    """Сколько минут до конца торгов (полная сессия MOEX, МСК)."""
    return _minutes_to(now, SESSION_END)


def _minutes_to(now: datetime | None, hhmm: str) -> int | None:
    now = now or datetime.now()
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return None
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return int((end - now).total_seconds() // 60)


def _timing_lines(d: dict) -> list[str]:
    """⏱ Когда именно случился скачок и успеваете ли вы."""
    tm = d.get("timing") or {}
    out: list[str] = []
    peak_t, low_t = tm.get("peak_time"), tm.get("low_time")
    since_low = tm.get("minutes_since_low")

    if peak_t and low_t:
        line = f"⏱ Скачок: <b>{peak_t} → {low_t}</b>"
        if since_low is not None:
            line += f" (дно {since_low} мин назад)"
        out.append(line)

    last_t, age = tm.get("last_data_time"), tm.get("data_age_min")
    now = dipscanner.now_msk()
    if last_t:
        age_txt = f", {age} мин назад" if isinstance(age, int) and age >= 0 else ""
        out.append(f"🕐 Данные с биржи: по <b>{last_t}</b>{age_txt} · сейчас {now:%H:%M}")

    left = _minutes_to_close(now)
    main_left = _minutes_to(now, MAIN_SESSION_END)
    if left is not None and left > 0:
        if main_left is not None and main_left <= 0:
            out.append(f"⏳ Основная сессия закрыта, идёт вечерняя — до конца {left // 60} ч "
                       f"{left % 60:02d} мин, ликвидность ниже")
        else:
            out.append(f"⏳ До конца торгов {left // 60} ч {left % 60:02d} мин")
    elif left is not None:
        out.append("⏳ Торги закрыты — заявку поставите утром, цена может уйти")

    # успеваете ли и не откупили ли уже
    rec = tm.get("recovered_pct")
    if since_low is not None and since_low > 30:
        tail = f", от дна уже +{_num(rec, 1)}%" if rec is not None and rec > 0 else ""
        out.append(f"⚠️ Скачок был {since_low} мин назад{tail} — отскок обычно в первые 30 мин, "
                   f"заходить уже позднее")
    elif since_low is not None and rec is not None and rec > 1.0:
        out.append(f"⚠️ От дна уже откупили +{_num(rec, 1)}% — вход хуже, чем был")
    elif since_low is not None:
        out.append("✅ Скачок свежий — вы успеваете поставить лимитную заявку")
    return out


def build_message(dips: list[dict], rating_map: dict[str, str], cfg: Config, minutes: int,
                  profiles: dict | None = None) -> str:
    """HTML-сообщение со списком провалов."""
    profiles = profiles or {}
    clean = [d for d in dips if not d["is_bad"]]
    bad = [d for d in dips if d["is_bad"]]

    reserve, invest, max_ideas = sizing.capital_split(
        cfg.my_capital, cfg.my_max_position_pct, cfg.my_reserve_pct)
    head = (f"🎯 <b>Резкие падения облигаций</b> — {len(dips)} шт. "
            f"(«чистых»: {len(clean)})")
    plan = (f"<i>Ваш капитал {_fmt_money(cfg.my_capital)} · на идею до "
            f"{_fmt_money(cfg.my_capital * cfg.my_max_position_pct / 100)} · "
            f"держать {cfg.hold_days} дн. или до +2% · одновременно не больше "
            f"{max_ideas} идей, резерв {_fmt_money(reserve)}</i>")
    if not cfg.only_rating_a_plus:
        plan += "\n<i>Без теста неквала берите только выпуски с рейтингом A+ (у остальных — максимум 100 тыс. ₽ по «праву последнего слова»)</i>"
    now_txt = dipscanner.now_msk().strftime("%H:%M")
    parts = [head,
             f"<i>MOEX, падение от максимума дня / за {minutes} мин · "
             f"сигнал собран в {now_txt} МСК</i>",
             plan, ""]

    def line(d: dict, clean_flag: bool) -> str:
        name = html.escape(str(d.get("name") or d["secid"]))
        price_pct = d.get("price_pct")
        rating = rating_map.get(d.get("isin") or "")
        rating_txt = f" · рейтинг {html.escape(rating)}" if rating else " · рейтинга A+ нет"
        icon = "🟢" if clean_flag else "⛔"
        txt = [
            f"{icon} <b>{name}</b> <code>{html.escape(d['secid'])}</code>",
            f"Падение <b>−{d['drop']:.1f}%</b> (день −{d['day_drop']:.1f}%)"
            + (f", за {minutes} мин −{d['win_drop']:.1f}%" if d.get("win_drop") else ""),
            f"Цена {price_pct:.1f}% от номинала{rating_txt}",
        ]
        if clean_flag:
            txt.extend(_timing_lines(d))
        # НКД и «сколько стоит лот» (НКД в рублях → в % от рублёвой цены лота)
        nkd = d.get("nkd")
        face = d.get("facevalue") or 1000
        rub_now = (price_pct / 100) * face if price_pct else 0
        if nkd:
            nkd_share = (nkd / rub_now * 100) if rub_now else None
            txt.append(f"НКД {_num(nkd, 2)} ₽/шт"
                       + (f" — {_num(nkd_share, 1)}% от цены (платите сверху)" if nkd_share else ""))
        if d.get("coupon_pct"):
            txt.append(f"Купон {_num(d['coupon_pct'], 1)}% годовых")
        # оборот/сделки/спред
        if clean_flag:
            txt.append(
                f"Сделок {d['numtrades']}, оборот {_fmt_money(d['valtoday'])}"
                + (f", спред {(d['offer'] - d['bid']) / d['bid'] * 100:.1f}%"
                   if d.get("bid") and d.get("offer") and d["bid"] > 0 else "")
            )
        else:
            txt.append("Причина отсева: " + html.escape("; ".join(d["reasons"][:2])))
        # сколько брать мне: считаем от доли капитала, лоты в рублях
        rub_price = rub_now
        if rub_price and cfg.my_capital:
            budget = cfg.my_capital * cfg.my_max_position_pct / 100
            lots = max(1, int(budget // (rub_price + (nkd or 0))))
            cost = lots * (rub_price + (nkd or 0))
            txt.append(f"🧮 <b>Вам:</b> {lots} шт ≈ {_fmt_money(cost)} "
                       f"({_num(cost / cfg.my_capital * 100, 1)}% капитала; бюджет на идею "
                       f"{_fmt_money(budget)})")
            txt.append(f"🎯 План: купить лимитной заявкой, продать через {cfg.hold_days} дн. "
                       f"или при +2% — по разбору его сделок 1–3 дня дают максимум")
        # расширенная аналитика
        for ln in formatter.analytics_lines(profiles.get(d.get("isin") or ""), price_pct=price_pct):
            txt.append(ln)
        url = f"https://snowball-income.com/public/asset/{d['secid']}.MCX.RUB"
        txt.append(f'🔗 <a href="{url}">карточка</a> · '
                   f'<a href="https://www.moex.com/ru/issue.aspx?code={d["secid"]}">MOEX</a>')
        return "\n".join(txt)

    if clean:
        parts.append("✅ <b>Похоже на панику, а не на дефолт</b>")
        parts.append("")
        if len(clean) > 6:
            parts.append(f"<i>(показываю 6 сильнейших из {len(clean)})</i>")
            parts.append("")
        for d in clean[:6]:
            parts.append(line(d, True))
            parts.append("➖➖➖➖➖➖➖➖")
    if bad:
        parts.append("")
        parts.append("⛔ <b>Отсеяно (не покупать в падении)</b>")
        for d in bad[:10]:
            parts.append(line(d, False))
            parts.append("")

    parts.append(
        "📌 <i>Напоминание: по замерам на этом портфеле медианный отскок через 2 часа "
        "+1,3%, но при входе с задержкой 10 минут — только +0,1%. Ловить нужно «в моменте», "
        "и лучше лимитной заявкой ниже рынка.</i>"
    )
    return "\n".join(parts)


def remember_cooldown(store, dips: list[dict], hours: int | None = None) -> None:
    """Записать сигналы в кулдаун, чтобы не повторяться (ручной запуск тоже)."""
    import time as _time

    alerts = store.get_meta("dip_alerts", {}) or {}
    now = _time.time()
    for d in dips:
        alerts[d.get("isin") or d["secid"]] = now
    store.set_meta("dip_alerts", alerts)
    try:
        store.flush()
    except Exception:  # noqa: BLE001
        pass


def run(cfg: Config, once: bool = True, dry_run: bool = False) -> int:
    store = open_store(cfg)
    notifiers = build_notifiers(cfg)

    moex_flags: dict[str, dict] = {}
    rating_map: dict[str, str] = {}

    dips = dipscanner.find_dips(
        store,
        moex_flags=moex_flags,
        min_drop_pct=cfg.dip_min_drop,
        window_minutes=cfg.dip_window,
        day_drop_pct=cfg.dip_day_drop,
        min_price_pct=cfg.dip_min_price_pct,
        min_trades=cfg.dip_min_trades,
        min_value=cfg.dip_min_value,
    )
    if not dips:
        log.info("Падений не найдено")
        store.close()
        return 0

    # антиспам: не повторять сигнал по одной бумаге чаще, чем раз в N часов
    import time as _time

    alerts = store.get_meta("dip_alerts", {}) or {}
    cooldown = cfg.dip_cooldown_hours * 3600
    now = _time.time()
    fresh, repeated = [], 0
    for d in dips:
        key = d.get("isin") or d["secid"]
        if cooldown and now - float(alerts.get(key, 0)) < cooldown:
            repeated += 1
            continue
        fresh.append(d)
    if repeated:
        log.info("Пропущено %d повторных сигналов (кулдаун %d ч)", repeated, cfg.dip_cooldown_hours)
    dips = fresh
    if not dips:
        store.close()
        return 0

    if cfg.dip_only_clean:
        clean_only = [d for d in dips if not d["is_bad"]]
        if not clean_only:
            log.info("Сигналы есть, но все отсеяны фильтрами (DIP_ONLY_CLEAN=1)")
            store.close()
            return 0
        dips = clean_only

    # режим «только рейтинг A+ и выше» — то, что неквал может покупать без теста
    if cfg.only_rating_a_plus:
        try:
            full = ratings_api.get_rating_map(store)
        except Exception:  # noqa: BLE001
            full = {}
        keep = [d for d in dips if not d["is_bad"] and (d.get("isin") in full)]
        log.info("ONLY_RATING_A_PLUS: из %d кандидатов осталось %d с рейтингом A+ и выше",
                 len(dips), len(keep))
        dips = keep
        if not dips:
            store.close()
            return 0

    # рейтинги — только для кандидатов, чтобы сообщение было информативнее
    if cfg.check_qualified:
        try:
            full = ratings_api.get_rating_map(store)
            for d in dips:
                if d.get("isin") in full:
                    rating_map[d["isin"]] = full[d["isin"]]
        except Exception as e:  # noqa: BLE001
            log.warning("Рейтинги недоступны: %s", e)

    profiles: dict[str, dict] = {}
    for d in dips:
        isin = d.get("isin")
        if not isin or d["is_bad"]:
            continue
        try:
            profiles[isin] = analytics.full_profile(isin, store=store)
        except Exception as e:  # noqa: BLE001
            log.warning("Аналитика по %s недоступна: %s", isin, e)

    text = build_message(dips, rating_map, cfg, cfg.dip_window, profiles)
    plain = formatter.to_plain(text)

    if dry_run:
        print(plain)
        store.close()
        return 0

    for d in dips:
        alerts[d.get("isin") or d["secid"]] = now
    store.set_meta("dip_alerts", alerts)

    # Telegram режет сообщения на 4096 символах — отправляем частями по блокам
    chunks = formatter.split_for_limit(text, limit=3900)
    sent = False
    for n in notifiers:
        for chunk in chunks:
            try:
                sent |= n.send(chunk, formatter.to_plain(chunk))
            except Exception as e:  # noqa: BLE001
                log.error("Отправка (%s) не удалась: %s", n.name, e)
    if not sent:
        log.error("Не удалось отправить сигнал")
        store.close()
        return 1
    log.info("Сигнал отправлен: %d бумаг", len(dips))
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    from .config import load_dotenv

    ap = argparse.ArgumentParser(description="Сканер резких падений облигаций")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    load_dotenv()
    cfg = Config()
    return run(cfg, once=True, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
