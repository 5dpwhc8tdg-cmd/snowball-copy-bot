"""Недельный отчёт: проверка фильтров на накопленных данных.

Зачем: вечерняя сводка отвечает «что было сегодня», но не отвечает на главный
вопрос — работает ли отбор. Здесь считаем по всему журналу (7 дней по умолчанию):

  1. все найденные падения — база;
  2. отправленные сигналы против НЕотправленных (контрольная группа);
  3. по вердиктам (брать / осторожно / поздний / слабый);
  4. по каждой причине отсева;
  5. сравнение с рынком: альфа = результат минус движение самой бумаги за день;
  6. риск: медиана и хвост просадки после входа;
  7. калибровка: попадает ли ожидание из rebound.py в реальность.

Запуск: python3 bot.py report [--days 7] [--dry-run]
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import statistics
from datetime import datetime, timedelta

from . import analytics, dipscanner, formatter, rebound
from .config import Config
from .notify import build_notifiers
from .store import open_store
from .summary import JOURNAL_KEY, day2_close, evaluate, journal_get

log = logging.getLogger(__name__)

KIND_LABEL = {"buy": "🟢 Брать", "maybe": "🟡 Осторожно", "late": "🟠 Поздно",
              "no": "⛔ Слабый сигнал", None: "— без вердикта"}


def _pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    s = f"{v:+.{digits}f}%"
    return s.replace(".", ",")


def _agg(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    return {
        "n": len(xs),
        "median": statistics.median(xs),
        "mean": statistics.mean(xs),
        "up": sum(1 for x in xs if x > 0) / len(xs) * 100,
        "p25": s[int(len(s) * 0.25)],
        "p75": s[int(len(s) * 0.75)],
        "worst": s[0],
        "best": s[-1],
    }


def _line(label: str, agg: dict, extra: str = "") -> str:
    if not agg.get("n"):
        return f"• {label}: нет данных"
    return (f"• <b>{label}</b> (n={agg['n']}): медиана <b>{_pct(agg['median'])}</b> · "
            f"в плюсе {agg['up']:.0f}% · p25 {_pct(agg['p25'])} · худший {_pct(agg['worst'])}{extra}")


def run_report(cfg: Config, days: int = 7, dry_run: bool = False) -> int:
    store = open_store(cfg)
    today = dipscanner.now_msk()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(1, days))]

    entries: list[dict] = []
    for day in reversed(dates):
        for e in journal_get(store, day):
            entries.append({**e, "day": day})
    if not entries:
        store.close()
        text = ("📈 <b>Недельный отчёт</b>\n\nЖурнал пуст: пока нечего считать. "
                "Отчёт наполнится, как только сканер поработает несколько дней.")
        if dry_run:
            print(formatter.to_plain(text))
        else:
            _send(cfg, text)
        return 0

    # считаем результат по каждому падению
    rows = []
    for e in entries:
        if not e.get("isin"):
            continue
        e["time"] = e.get("time")
        res = evaluate(e)
        if not res:
            continue
        d2 = day2_close(e["isin"], e["day"])
        if d2:
            res["day2"] = (d2 - res["entry_price_pct"]) / res["entry_price_pct"] * 100
        rows.append({**e, **res})

    sent = [r for r in rows if r.get("sent")]
    unsent = [r for r in rows if not r.get("sent")]
    ok = [r for r in rows if r.get("eod") is not None]

    parts = [f"📈 <b>Отчёт за {days} дн.</b> — {today:%d.%m.%Y}",
             f"<i>Найдено падений: {len(entries)} · оценено: {len(ok)} · "
             f"отправлено сигналов: {len(sent)}</i>", ""]

    # 1. отправленные против контрольной группы
    parts.append("📬 <b>Сигналы против контроля</b>")
    parts.append(_line("отправленные", _agg([r["eod"] for r in sent])))
    parts.append(_line("не отправлены", _agg([r["eod"] for r in unsent])))
    parts.append("")

    # 2. по вердиктам
    parts.append("🧭 <b>По вердиктам (к концу дня)</b>")
    for kind in ("buy", "maybe", "late", "no"):
        xs = [r["eod"] for r in ok if r.get("kind") == kind]
        if xs:
            alpha = [r["eod"] - (r.get("day_move_from_entry") or 0) for r in ok
                     if r.get("kind") == kind]
            parts.append(_line(KIND_LABEL[kind], _agg(xs)))
    parts.append("")

    # 3. по причинам отсева
    groups: dict[str, list[float]] = {}
    for r in ok:
        if r.get("is_bad"):
            key = "отсеяно фильтрами сканера"
        elif r.get("kind") == "no":
            key = "слабый сигнал (баллы)"
        else:
            continue
        groups.setdefault(key, []).append(r["eod"])
    if groups:
        parts.append("🚫 <b>Что отсеяли — и что оно дало</b>")
        for key, xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            parts.append(_line(key, _agg(xs)))
        parts.append("")

    # 4. риск
    parts.append("⚠️ <b>Риск после входа</b>")
    for kind in ("buy", "maybe", "late", "no"):
        xs = [r["low_after"] for r in ok if r.get("kind") == kind]
        if xs:
            parts.append(f"• {KIND_LABEL[kind]}: медиана просадки {_pct(statistics.median(xs))}, "
                         f"худшая {_pct(min(xs))}")
    parts.append("")

    # 4.5. срабатывание цели +1% (главное правило после замеров)
    tgt = [r.get("target_2h") for r in ok if r.get("target_2h") is not None]
    if tgt:
        hit = sum(1 for x in tgt if x)
        parts.append("🎯 <b>Цель +1% в первые 2 часа</b>")
        parts.append(f"• сработала бы в {hit} из {len(tgt)} сигналов ({hit / len(tgt) * 100:.0f}%)")
        if sent:
            sent_hit = [r.get("target_2h") for r in sent if r.get("target_2h") is not None]
            if sent_hit:
                parts.append(f"• по отправленным: {sum(1 for x in sent_hit if x)} из {len(sent_hit)}")
        parts.append("")

    # 5. на 2-й день
    d2 = [r["day2"] for r in ok if r.get("day2") is not None]
    if d2:
        parts.append("🗓 <b>Держим до 2-го дня</b>")
        parts.append(_line("все сигналы", _agg(d2)))
        parts.append("")

    # 6. калибровка ожиданий
    calib = {}
    for r in ok:
        est = rebound.estimate(r.get("minutes_since_low"), r.get("drop") or 4)
        bucket = est["lag"] // 30 * 30
        calib.setdefault(bucket, []).append(r["eod"])
    if calib:
        parts.append("🎯 <b>Калибровка ожиданий</b> (ожидание → факт, к концу дня)")
        for bucket in sorted(calib):
            xs = calib[bucket]
            exp = rebound.estimate(bucket, 5.0)["expect_pct"]
            parts.append(f"• опоздание ~{bucket} мин: ожидали ~{_pct(exp)}, "
                         f"факт медиана {_pct(statistics.median(xs))} (n={len(xs)})")
        parts.append("")

    parts.append("📌 <i>Оценка без комиссии, спреда и налога. Малая выборка — "
                 "смотреть на тренд, а не на отдельные цифры.</i>")

    text = "\n".join(parts)
    store.close()
    if dry_run:
        print(formatter.to_plain(text))
        return 0
    _send(cfg, text)
    log.info("Отчёт отправлен: событий %d", len(entries))
    return 0


def _send(cfg: Config, text: str) -> None:
    for n in build_notifiers(cfg):
        for chunk in formatter.split_for_limit(text, limit=3900):
            try:
                n.send(chunk, formatter.to_plain(chunk))
            except Exception as e:  # noqa: BLE001
                log.error("Отправка (%s): %s", n.name, e)
