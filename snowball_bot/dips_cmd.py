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

from . import analytics, dipscanner, formatter, ratings as ratings_api, rebound as rebound_lib, sizing, verdict as verdict_mod
from .config import Config
from .notify import build_notifiers
from .store import open_store

log = logging.getLogger(__name__)


def _num(value, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _points(n: int) -> str:
    """Баллы по-русски: 1 балл, 2 балла, 5 баллов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        word = "балл"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "балла"
    else:
        word = "баллов"
    return f"{n} {word}"


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


def _breadth_lines(breadth: dict | None) -> list[str]:
    """🌡 Широта рынка: общая паника или отдельная бумага."""
    if not breadth or not breadth.get("total"):
        return []
    total, down, deep = breadth["total"], breadth["down"], breadth.get("deep", 0)
    share = breadth.get("down_share") or 0
    med = breadth.get("median") or 0
    worst = breadth.get("worst") or 0

    if share >= 65:
        verdict = ("🔴 общая паника — падает почти всё: скорее ставка или новости рынка, "
                   "чем проблема конкретного выпуска")
    elif share >= 40:
        verdict = "🟡 смешанный рынок — падение частичное, смотрите историю самой бумаги"
    else:
        verdict = ("🟢 рынок в целом спокоен — падение индивидуальное, "
                   "важнее собственная статистика отскоков у бумаги")

    med_txt = _num(med, 2)
    if med_txt in ("-0,00", "-0,0"):
        med_txt = med_txt.lstrip("-")
    line = (f"🌡 <b>Рынок:</b> в минусе {down} из {total} бумаг ({_num(share, 0)}%), "
            f"глубоко (−2% и ниже) — {deep}, медиана {med_txt}%, худшая {_num(worst, 1)}%")
    return [line, f"<i>{verdict}</i>"]


def build_message(dips: list[dict], rating_map: dict[str, str], cfg: Config, minutes: int,
                  profiles: dict | None = None, breadth: dict | None = None) -> str:
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
    # счёт по вердиктам — сразу видно, есть ли что-то к покупке
    kinds = {}
    for d in clean[:6]:
        v = verdict_mod.decide(d, profiles.get(d.get("isin") or "") or {}, breadth, cfg)
        kinds[v.kind] = kinds.get(v.kind, 0) + 1
    if kinds:
        summary_line = " · ".join(
            f"{verdict_mod.ICON[k]} {verdict_mod.LABEL[k].lower()}: {kinds[k]}"
            for k in (verdict_mod.BUY, verdict_mod.MAYBE, verdict_mod.LATE, verdict_mod.NO)
            if kinds.get(k))
        head += f"\n<b>Итог:</b> {summary_line}"

    parts = [head,
             f"<i>MOEX, падение от максимума дня / за {minutes} мин · "
             f"сигнал собран в {now_txt} МСК</i>",
             plan]
    parts.extend(_breadth_lines(breadth))
    parts.append("")

    def line(d: dict, clean_flag: bool) -> str:
        """Карточка с «воздухом»: каждая мысль — отдельная строка со смайликом."""
        name = html.escape(str(d.get("name") or d["secid"]))
        price_pct = d.get("price_pct") or 0
        rating = rating_map.get(d.get("isin") or "")
        prof = profiles.get(d.get("isin") or "") or {}
        v = verdict_mod.decide(d, prof, breadth, cfg)
        tm = d.get("timing") or {}
        out: list[str] = []

        # ── заголовок: вердикт и имя ──────────────────────────────────────
        out.append(f"{v.icon} <b>{v.label}</b> — <b>{name}</b>")
        sign = "−" if v.score < 0 else "+"
        out.append(f"<code>{html.escape(d['secid'])}</code> · {sign}{_points(v.score)}")
        out.append("")

        # ── что случилось ─────────────────────────────────────────────────
        when = ""
        if tm.get("peak_time") and tm.get("low_time"):
            when = f"<code>{tm['peak_time']} → {tm['low_time']}</code>"
            if tm.get("minutes_since_low") is not None:
                when += f" · {tm['minutes_since_low']} мин назад"
        out.append(f"📉 <b>Падение −{_num(d['drop'], 1)}%</b> · "
                   f"цена {_num(price_pct, 1)}% от номинала")
        if when:
            out.append(f"⏱ {when}")
        out.append("")

        # ── почему нельзя брать ───────────────────────────────────────────
        if v.blocks:
            out.append(f"🚫 <b>Причина:</b> {html.escape('; '.join(v.blocks))}")
            out.append("")

        # ── деньги ────────────────────────────────────────────────────────
        face = d.get("facevalue") or 1000
        rub_now = (price_pct / 100) * face
        nkd = d.get("nkd") or 0
        lot_txt = _fmt_money(rub_now)
        if nkd:
            lot_txt += f" + НКД {_num(nkd, 2)} ₽"
        out.append(f"💵 Лот: {lot_txt}")
        budget = cfg.my_capital * cfg.my_max_position_pct / 100 if cfg.my_capital else 0
        lots = max(1, int(budget // (rub_now + nkd))) if (budget and rub_now) else 0
        if lots and v.size_pct and not v.blocks:
            lots_adj = max(1, int(lots * v.size_pct / 100))
            size_txt = (f"🧮 <b>Вам: {lots_adj} шт ≈ {_fmt_money(lots_adj * (rub_now + nkd))}</b>"
                        f" ({_num(lots_adj * (rub_now + nkd) / cfg.my_capital * 100, 1)}% капитала)")
            if v.size_pct < 100:
                size_txt += f"\n   <i>это {v.size_pct}% обычного размера — вход поздний</i>"
            out.append(size_txt)
        out.append("")

        # ── ожидание (только если бумагу реально можно брать) ─────────────
        if clean_flag and v.kind != verdict_mod.NO:
            out.append("🎯 <b>Ожидание:</b> " + rebound_lib.explain(tm.get("minutes_since_low"), d["drop"]))
            out.append("")

        # ── качество бумаги ───────────────────────────────────────────────
        avg_val = prof.get("avg_value_5d")
        liq = []
        if avg_val:
            liq.append(f"оборот {_fmt_money(avg_val)}/день")
        elif d.get("valtoday"):
            liq.append(f"оборот дня {_fmt_money(d['valtoday'])}")
        if d.get("bid") and d.get("offer") and d["bid"] > 0:
            liq.append(f"спред {(d['offer'] - d['bid']) / d['bid'] * 100:.1f}%")
        liq.append(f"рейтинг {rating}" if rating else "рейтинга A+ нет")
        out.append("📊 " + " · ".join(liq))

        stats = []
        if prof.get("prev_drops") is not None:
            med = prof.get("prev_drops_rebound_median_pct")
            if med is None:
                stats.append("таких падений у неё раньше не было")
            else:
                stats.append(f"её падения: {prof['prev_drops']} шт, медиана 2 дн. "
                             f"{med:+.1f}%, в плюсе {prof['prev_drops_up_share_pct']:.0f}%")
        if prof.get("drawdown_from_max_pct") is not None:
            stats.append(f"{prof['drawdown_from_max_pct']:.0f}% от годового максимума")
        if breadth and breadth.get("down_share") is not None:
            stats.append(f"на рынке в минусе {breadth['down_share']:.0f}%")
        if stats:
            out.append("🔁 " + " · ".join(stats))
        out.append("")

        # ── почему такой вердикт + ссылки ─────────────────────────────────
        why = v.short(2) if not v.blocks else ""
        if why:
            out.append(f"<i>{html.escape(why)}</i>")
        out.append(f'🔗 <a href="https://www.moex.com/ru/issue.aspx?code={d["secid"]}">MOEX</a>'
                   f' · <a href="https://snowball-income.com/public/asset/{d["secid"]}.MCX.RUB">карточка</a>')
        return "\n".join(out)

    if clean:
        parts.append("✅ <b>Похоже на панику, а не на дефолт</b>")
        parts.append("")
        if len(clean) > 6:
            parts.append(f"<i>(показываю 6 сильнейших из {len(clean)})</i>")
            parts.append("")
        for d in clean[:6]:
            parts.append(line(d, True))
            parts.append("➖➖➖➖➖➖")
    if bad:
        parts.append("")
        parts.append("⛔ <b>Отсеяно (не покупать в падении)</b>")
        for d in bad[:5]:
            parts.append(line(d, False))

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

    # журнал дня — из него вечером считается сводка
    try:
        from . import summary

        summary.journal_add(store, dips, cfg, profiles)
    except Exception as e:  # noqa: BLE001
        log.warning("Не смог записать журнал сигналов: %s", e)
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

    breadth = store.get_meta("market_breadth", {}) or {}
    # вердикт по каждой бумаге — кладём в кандидата, чтобы он попал и в журнал
    for d in dips:
        try:
            v = verdict_mod.decide(d, profiles.get(d.get("isin") or "") or {}, breadth, cfg)
            d["verdict_kind"] = v.kind
        except Exception:  # noqa: BLE001
            pass
    text = build_message(dips, rating_map, cfg, cfg.dip_window, profiles, breadth)
    plain = formatter.to_plain(text)

    if dry_run:
        print(plain)
        store.close()
        return 0

    for d in dips:
        alerts[d.get("isin") or d["secid"]] = now
    store.set_meta("dip_alerts", alerts)

    # журнал дня — из него вечером считается сводка
    try:
        from . import summary

        summary.journal_add(store, dips, cfg, profiles)
    except Exception as e:  # noqa: BLE001
        log.warning("Не смог записать журнал сигналов: %s", e)

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
