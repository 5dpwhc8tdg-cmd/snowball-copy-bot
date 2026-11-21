"""Долгоживущий слушатель команд в Telegram (Telegram Long Polling).

Зачем нужен отдельный процесс, если есть бот на GitHub Actions:
  Actions запускается по расписанию и «спит» между запусками — команду в чате
  ему не передать. Этот файл держит постоянное соединение с Telegram
  (getUpdates) и выполняет команды из чата сразу:

    /status  — состояние: капитал, сделки в базе, время последней проверки
    /now     — принудительная проверка портфеля (как workflow «Run workflow»)
    /dips    — принудительный скан падений прямо сейчас, в обход кулдауна
    /data    — проверка источников данных (Snowball API, MOEX ISS, рейтинги)
    /cap 120000 — изменить ваш капитал (пересчёт размеров сделок)
    /help

Запуск:
    python3 commands.py            # локально / на любой машине
    — или workflow .github/workflows/commands.yml (ручной старт, ~5.5 часа)

Секреты: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (тот же, что у бота).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

from snowball_bot import api, dipscanner, formatter, ratings as ratings_api, sizing
from snowball_bot.config import Config, load_dotenv
from snowball_bot.dipscanner import find_dips
from snowball_bot.main import build_notifiers, check_once, open_store, setup_logging
from snowball_bot.notify import TelegramNotifier

log = logging.getLogger("snowball_bot.commands")

HELP = (
    "🤖 <b>Snowball Copy Bot — команды</b>\n\n"
    "/now — принудительно проверить новые сделки прямо сейчас\n"
    "/dips — принудительный скан падений (в обход кулдауна)\n"
    "/data — проверить, что источники данных отвечают\n"
    "/status — состояние: капитал, сделки в базе, время проверки\n"
    "/last — 5 последних сделок с аналитикой и размером под ваш капитал\n"
    "/cap 120000 — изменить ваш капитал, ₽\n"
    "/plan — раскладка капитала (резерв, бюджет идеи, число идей)\n"
    "/summary — сводка дня: что было бы, если купить по сегодняшним сигналам\n"
    "/report — отчёт за неделю: проверка правил на накопленных данных\n"
    "/mute — пауза уведомлений на 2 часа\n"
    "/unmute — снять паузу\n"
    "/help — эта справка"
)


class TelegramAPI:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, params: dict | None = None, timeout: int = 40) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send(self, chat_id: str, text: str) -> bool:
        # Telegram режет >4096 символов — отправляем частями
        for chunk in formatter.split_for_limit(text, limit=3900):
            try:
                self.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            except Exception as e:  # noqa: BLE001
                log.error("sendMessage: %s", e)
                return False
        return True


class CommandBot:
    def __init__(self, cfg: Config, api_client: TelegramAPI):
        self.cfg = cfg
        self.api = api_client
        self.allowed = {str(c) for c in cfg.telegram_chat_ids}
        self.store = open_store(cfg)
        self.offset = int(self.store.get_meta("cmd_offset", 0) or 0)
        self.busy: set[str] = set()

    # --- утилиты -------------------------------------------------------
    def refresh_state(self) -> bool:
        """Подтянуть свежее состояние из репозитория перед чтением.

        Слушатель живёт до 6 часов, и всё это время он видит состояние,
        зафиксированное при старте job'а. Из-за этого /summary отвечал
        «сигналов не было», хотя сканер их уже записал. Перед чтением
        подтягиваем origin/main (быстро и без риска).
        """
        import subprocess
        import os

        try:
            # сначала сохраняем то, что накопили сами (offset команд и т.п.)
            try:
                self.store.flush()
            except Exception:  # noqa: BLE001
                pass
            if os.environ.get("GITHUB_ACTIONS"):
                subprocess.run(["git", "fetch", "--quiet", "origin", "HEAD"], timeout=25)
                subprocess.run(["git", "checkout", "--quiet", "FETCH_HEAD", "--", "state"],
                               timeout=25, check=False)
                # файл обновился — перечитываем его в память, иначе видим старое
                self.store.reload()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог обновить состояние из репозитория: %s", e)
            return False

    def reply(self, chat_id: str, text: str) -> None:
        self.api.send(chat_id, text)

    def runtime_capital(self) -> float:
        return float(self.store.get_meta("my_capital", self.cfg.my_capital) or self.cfg.my_capital)

    def plan_text(self) -> str:
        cap = self.runtime_capital()
        reserve, invest, max_ideas = sizing.capital_split(
            cap, self.cfg.my_max_position_pct, self.cfg.my_reserve_pct
        )
        per_idea = cap * self.cfg.my_max_position_pct / 100
        return (
            f"💰 <b>Ваш капитал: {formatter._fmt_money(cap)}</b>\n"
            f"• резерв: <b>{formatter._fmt_money(reserve)}</b> ({self.cfg.my_reserve_pct:g}%)\n"
            f"• рабочие деньги: <b>{formatter._fmt_money(invest)}</b>\n"
            f"• максимум на одну идею: <b>{formatter._fmt_money(per_idea)}</b> "
            f"({self.cfg.my_max_position_pct:g}%)\n"
            f"• одновременно идей: не больше <b>{max_ideas}</b>\n"
            f"• держим <b>{self.cfg.hold_days} дн.</b> или до +2%\n"
            f"• масштаб от владельца: 1 : {self.cfg.owner_capital / cap:.0f}"
        )

    # --- команды -------------------------------------------------------
    def cmd_status(self, chat_id: str) -> None:
        self.refresh_state()
        muted = bool((self.store.get_meta("mute_until", 0) or 0) > time.time())
        self.reply(
            chat_id,
            "📟 <b>Статус</b>\n"
            f"Портфель: <b>{self.cfg.portfolio_title}</b>\n"
            f"Сделок в базе: {self.store.seen_count()}\n"
            f"Последняя проверка: {self.store.get_meta('last_check_human', '—')}\n"
            f"Уведомления: {'на паузе' if muted else 'включены'}\n"
            f"Только A+: {'да' if self.cfg.only_rating_a_plus else 'нет'}\n\n"
            + self.runtime_capital_text(),
        )

    def runtime_capital_text(self) -> str:
        return f"💰 Капитал: <b>{formatter._fmt_money(self.runtime_capital())}</b>"

    def cmd_data(self, chat_id: str) -> None:
        self.reply(chat_id, "🔍 Проверяю источники данных…")
        lines = ["🔍 <b>Источники данных</b>"]
        # 1) Snowball API
        try:
            t0 = time.time()
            rows = api.fetch_all_transactions(self.cfg.portfolio_id, max_pages=1, page_size=20)
            tx_types = {r.get("type") for r in rows}
            lines.append(
                f"✅ Snowball API: {len(rows)} сделок за {time.time() - t0:.1f} с "
                f"(типы: {sorted(t for t in tx_types if t)})"
            )
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ Snowball API: {e}")
        # 2) MOEX ISS
        try:
            t0 = time.time()
            snap = dipscanner.snapshot()
            lines.append(
                f"✅ MOEX ISS: {len(snap)} облигаций (TQCB) за {time.time() - t0:.1f} с"
            )
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ MOEX ISS: {e}")
        # 3) рейтинги
        try:
            rmap = ratings_api.get_rating_map(self.store)
            lines.append(f"✅ Рейтинги (smart-lab): {len(rmap)} выпусков в кэше")
        except Exception as e:  # noqa: BLE001
            lines.append(f"⚠️ Рейтинги: {e}")
        journal = self.store.get_meta("dip_journal", {}) or {}
        today = dipscanner.now_msk().strftime("%Y-%m-%d")
        lines.append(f"🗂 Журнал за сегодня: {len(journal.get(today) or [])} записей")
        lines.append(f"\n🕒 {dipscanner.now_msk():%d.%m.%Y %H:%M:%S} МСК")
        self.reply(chat_id, "\n".join(lines))

    def cmd_now(self, chat_id: str) -> None:
        if "now" in self.busy:
            self.reply(chat_id, "⏳ Проверка уже идёт, подождите…")
            return
        self.busy.add("now")
        try:
            self.reply(chat_id, "⏳ Проверяю портфель…")
            notifiers = build_notifiers(self.cfg)
            n = check_once(self.cfg, self.store, notifiers)
            if not n:
                self.reply(chat_id, "✅ Проверка прошла: новых сделок нет (уведомление не нужно).")
            else:
                self.reply(chat_id, f"📬 Новых сделок: {n} — карточка отправлена выше.")
        except Exception as e:  # noqa: BLE001
            log.error("now: %s", e)
            self.reply(chat_id, f"❌ Не смог проверить портфель: {e}")
        finally:
            self.busy.discard("now")

    def cmd_dips(self, chat_id: str) -> None:
        if not getattr(self.cfg, "dip_enabled", False):
            self.reply(chat_id,
                       "📴 <b>Сигналы по падениям выключены</b>\n\n"
                       "Замеры на 95 днях и 776 событиях дали ожидание около нуля "
                       "(−0,14% на сделку): ловить отскоки убыточно.\n\n"
                       "Журнал падений продолжает копиться — вернусь с проверкой, "
                       "когда наберётся свежая статистика. Включить вручную: "
                       "команда с DIP_ENABLED=1.")
            return
        if "dips" in self.busy:
            self.reply(chat_id, "⏳ Скан уже идёт, подождите…")
            return
        self.busy.add("dips")
        try:
            self.reply(chat_id, "⏳ Сканирую рынок облигаций на провалы…")
            dips = find_dips(
                self.store,
                moex_flags={},
                min_drop_pct=self.cfg.dip_min_drop,
                window_minutes=self.cfg.dip_window,
                day_drop_pct=self.cfg.dip_day_drop,
                min_price_pct=self.cfg.dip_min_price_pct,
                min_trades=self.cfg.dip_min_trades,
                min_value=self.cfg.dip_min_value,
            )
            if not dips:
                self.reply(chat_id, "✅ Падений по текущим фильтрам не найдено. Рынок спокоен.")
                return
            from snowball_bot import dips_cmd

            clean = [d for d in dips if not d["is_bad"]]
            rating_map: dict[str, str] = {}
            profiles: dict[str, dict] = {}
            try:
                rmap = ratings_api.get_rating_map(self.store)
                for d in clean:
                    if d.get("isin") in rmap:
                        rating_map[d["isin"]] = rmap[d["isin"]]
                for d in clean[:6]:
                    if d.get("isin"):
                        try:
                            profiles[d["isin"]] = dips_cmd.analytics.full_profile(
                                d["isin"], store=self.store
                            )
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as e:  # noqa: BLE001
                log.warning("рейтинги/профили: %s", e)
            # cmd_dips игнорирует кулдаун — это ручной запуск
            breadth = self.store.get_meta("market_breadth", {}) or {}
            text = dips_cmd.build_message(
                clean or dips, rating_map, self.cfg, self.cfg.dip_window, profiles, breadth
            )
            self.api.send(chat_id, text)
            dips_cmd.remember_cooldown(self.store, clean or dips)
        except Exception as e:  # noqa: BLE001
            log.error("dips: %s", e)
            self.reply(chat_id, f"❌ Скан не удался: {e}")
        finally:
            self.busy.discard("dips")

    def cmd_cap(self, chat_id: str, arg: str) -> None:
        raw = (arg or "").replace(" ", "").replace("₽", "").replace(".", "").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            self.reply(chat_id, "Формат: <code>/cap 120000</code> — капитал в рублях.")
            return
        if not (1000 <= value <= 100_000_000):
            self.reply(chat_id, "Капитал должен быть от 1 000 до 100 000 000 ₽.")
            return
        self.store.set_meta("my_capital", value)
        self.store.flush()
        self.reply(chat_id, f"✅ Капитал обновлён.\n\n{self.plan_text()}")

    # --- цикл ----------------------------------------------------------
    def handle(self, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("start", "help"):
            self.reply(chat_id, HELP)
        elif cmd == "status":
            self.cmd_status(chat_id)
        elif cmd == "data":
            threading.Thread(target=self.cmd_data, args=(chat_id,), daemon=True).start()
        elif cmd in ("now", "check", "update"):
            threading.Thread(target=self.cmd_now, args=(chat_id,), daemon=True).start()
        elif cmd in ("dips", "dip"):
            threading.Thread(target=self.cmd_dips, args=(chat_id,), daemon=True).start()
        elif cmd in ("cap", "capital"):
            self.cmd_cap(chat_id, arg)
        elif cmd == "plan":
            self.reply(chat_id, self.plan_text())
        elif cmd == "mute":
            self.store.set_meta("mute_until", int(time.time()) + 7200)
            self.store.flush()
            self.reply(chat_id, "🔇 Уведомления о сделках на паузе 2 часа "
                                "(сигналы о падениях продолжат приходить).")
        elif cmd == "unmute":
            self.store.set_meta("mute_until", 0)
            self.store.flush()
            self.reply(chat_id, "🔔 Уведомления включены.")
        elif cmd in ("report", "week"):
            threading.Thread(target=self.cmd_report, args=(chat_id,), daemon=True).start()
        elif cmd in ("summary", "day"):
            threading.Thread(target=self.cmd_summary, args=(chat_id, arg), daemon=True).start()
        elif cmd == "last":
            threading.Thread(target=self.cmd_last, args=(chat_id,), daemon=True).start()
        else:
            self.reply(chat_id, "Не понимаю команду.\n\n" + HELP)

    def cmd_summary(self, chat_id: str, arg: str = "") -> None:
        try:
            from snowball_bot import summary as summary_mod

            date = arg.strip() if arg and arg.strip()[:2] == "20" else None
            self.refresh_state()
            day = date or dipscanner.now_msk().strftime("%Y-%m-%d")
            self.reply(chat_id, "⏳ Считаю сводку по свечам…" + (f" ({date})" if date else ""))
            summary_mod.run(self.cfg, days=1, dry_run=False, date=date)
            # если журнал пуст — говорим, где искать, а не «сигналов не было»
            journal = (self.store.get_meta("dip_journal", {}) or {})
            if not journal.get(day):
                days = sorted((d for d, v in journal.items() if v), reverse=True)[:3]
                hint = ", ".join(f"/summary {d}" for d in days) or "журнал пока пуст"
                self.reply(chat_id, f"ℹ️ В журнале нет записей за {day}. Есть за: {hint}")
        except Exception as e:  # noqa: BLE001
            log.error("summary: %s", e)
            self.reply(chat_id, f"❌ Не смог собрать сводку: {e}")

    def cmd_report(self, chat_id: str) -> None:
        try:
            from snowball_bot import report as report_mod

            self.refresh_state()

            self.reply(chat_id, "⏳ Считаю отчёт за неделю по свечам…")
            report_mod.run_report(self.cfg, days=7)
        except Exception as e:  # noqa: BLE001
            log.error("report: %s", e)
            self.reply(chat_id, f"❌ Не смог собрать отчёт: {e}")

    def cmd_last(self, chat_id: str) -> None:
        try:
            from snowball_bot.main import check_once  # noqa: F401  (форматтер уже умеет)

            rows = api.fetch_all_transactions(self.cfg.portfolio_id, max_pages=1, page_size=5)
            if not rows:
                self.reply(chat_id, "Сделок не найдено.")
                return
            cap = self.runtime_capital()
            for r in rows[:5]:
                size = sizing.compute(
                    his_qty=r.get("amount") or 0,
                    price_per_unit=r.get("price") or 0,
                    nkd_per_unit=r.get("nkd") or 0,
                    my_capital=cap,
                    his_capital=self.cfg.owner_capital,
                    max_position_pct=self.cfg.my_max_position_pct,
                )
                text = formatter.format_tx(
                    r,
                    tz=self.cfg.tz,
                    portfolio_title=self.cfg.portfolio_title,
                    portfolio_url=self.cfg.snowball_url,
                    my_size=size,
                )
                self.api.send(chat_id, text)
        except Exception as e:  # noqa: BLE001
            self.reply(chat_id, f"❌ Не смог получить сделки: {e}")

    def run(self) -> None:
        # снимаем «висящие» апдейты, накопленные пока процесса не было
        try:
            pending = self.api.call("getUpdates", {"offset": -1, "timeout": 0})
            res = pending.get("result") or []
            if res:
                self.offset = res[-1]["update_id"] + 1
                self.store.set_meta("cmd_offset", self.offset)
                self.store.flush()
        except Exception as e:  # noqa: BLE001
            log.debug("getUpdates(offset=-1): %s", e)

        log.info("Слушаю команды в Telegram (Ctrl+C — выход)")
        started = time.time()
        while True:
            if time.time() - started > 5.85 * 3600:  # под лимит job в Actions (6 ч)
                log.info("Отработал 5.85 ч (лимит job'а 6 ч) — выхожу, перезапуск по расписанию")
                return
            try:
                data = self.api.call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 30,
                        "allowed_updates": ["message"],
                    },
                    timeout=45,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates: %s", e)
                time.sleep(5)
                continue
            for upd in data.get("result") or []:
                self.offset = upd["update_id"] + 1
                self.store.set_meta("cmd_offset", self.offset)
                self.store.flush()
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str((msg.get("chat") or {}).get("id") or "")
                if not text or not chat_id or chat_id not in self.allowed:
                    continue
                log.info("Команда из чата %s: %s", chat_id, text[:60])
                if chat_id not in self.allowed:
                    log.warning("Чужой чат %s — игнорирую", chat_id)
                    continue
                try:
                    self.handle(chat_id, text)
                except Exception:  # noqa: BLE001
                    log.error("Ошибка обработки команды:\n%s", traceback.format_exc())
                    self.reply(chat_id, "❌ Внутренняя ошибка при выполнении команды.")


def poll_once(bot: "CommandBot") -> int:
    """Один проход по чату: выполнить все накопившиеся команды и выйти.

    Так работает GitHub Actions: процесс живёт минуту, поэтому команду «/now»
    из чата забирает ближайший запуск по расписанию (каждые 5 минут).
    """
    try:
        data = bot.api.call("getUpdates", {"offset": bot.offset, "timeout": 0}, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.warning("getUpdates: %s", e)
        return 0
    handled = 0
    for upd in data.get("result") or []:
        bot.offset = upd["update_id"] + 1
        bot.store.set_meta("cmd_offset", bot.offset)
        bot.store.flush()
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if not text or not chat_id or chat_id not in bot.allowed:
            continue
        log.info("Команда из чата %s: %s", chat_id, text[:60])
        handled += 1
        try:
            bot.handle(chat_id, text)
        except Exception:  # noqa: BLE001
            log.error("Ошибка обработки команды:\n%s", traceback.format_exc())
            bot.reply(chat_id, "❌ Внутренняя ошибка при выполнении команды.")
    if handled:
        log.info("Выполнено команд: %d", handled)
    return handled


def main() -> int:
    load_dotenv(os.environ.get("ENV_FILE", ".env"))
    cfg = Config()
    setup_logging(cfg)
    if not cfg.telegram_token or not cfg.telegram_chat_ids:
        log.error("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return 2
    bot = CommandBot(cfg, TelegramAPI(cfg.telegram_token))
    try:
        if "--once" in sys.argv[1:]:
            poll_once(bot)
        else:
            bot.run()
    finally:
        bot.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
