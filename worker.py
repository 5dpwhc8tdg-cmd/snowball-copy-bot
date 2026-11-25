"""Долгоживущий процесс для VPS/PaaS: слушатель команд + проверка сделок.

Зачем: на GitHub Actions команды из чата работают с задержкой (job живёт до 6 ч,
cron тормозит, бывают очереди). На любом постоянно работающем хосте всё это
исчезает — команда выполняется за секунды.

Что делает процесс:
  * слушает Telegram (getUpdates) и выполняет команды /now, /dips, /data,
    /summary, /report, /status, /plan, /last, /cap, /mute;
  * параллельно проверяет портфель на новые сделки каждые POLL_INTERVAL секунд;
  * состояние хранит в JSON-файле STATE_PATH (тот же формат, что в репозитории),
    так что при переезде ничего не теряется.

Запуск:
    python3 worker.py

Переменные окружения — как у бота (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, …).
Дополнительно:
    STATE_BACKEND=json STATE_PATH=/data/state.json
    WORKER_CHECK=1        # 1 = проверять сделки (по умолчанию 1)
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from snowball_bot import dipscanner
from snowball_bot.config import Config, load_dotenv
from snowball_bot.main import build_notifiers, check_once, open_store, setup_logging
from snowball_bot.store import open_store as _open_store  # noqa: F401  (единая точка)

import commands as cmd_module

log = logging.getLogger("snowball_bot.worker")
STOP = threading.Event()


def portfolio_loop(cfg: Config, store) -> None:
    """Проверка новых сделок портфеля по расписанию."""
    notifiers = build_notifiers(cfg)
    failures = 0
    log.info("Проверка портфеля каждые %d сек", cfg.poll_interval)
    while not STOP.is_set():
        try:
            n = check_once(cfg, store, notifiers)
            store.set_meta("last_check_human", time.strftime("%d.%m.%Y %H:%M:%S"))
            store.flush()
            if n:
                log.info("Сделок обработано: %d", n)
            failures = 0
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Проверка портфеля не удалась (%d): %s", failures, e)
            if failures in (5, 20):
                for nt in notifiers:
                    try:
                        nt.send(f"⚠️ Не могу получить данные портфеля: {e}",
                                f"⚠️ Не могу получить данные: {e}")
                    except Exception:  # noqa: BLE001
                        pass
            STOP.wait(min(60 * failures, 900))
            continue
        STOP.wait(cfg.poll_interval)


def main() -> int:
    load_dotenv(os.environ.get("ENV_FILE", ".env"))
    cfg = Config()
    setup_logging(cfg)

    if not cfg.telegram_token or not cfg.telegram_chat_ids:
        log.error("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return 2

    log.info("Портфель: %s (%s)", cfg.portfolio_title, cfg.portfolio_id)
    log.info("Состояние: %s (%s)", cfg.state_path, cfg.state_backend)
    log.info("Время МСК сейчас: %s", dipscanner.now_msk().strftime("%H:%M:%S"))

    store = open_store(cfg)
    # max_hours=0 — слушаем бесконечно (на VPS нет лимита job'а GitHub)
    listener = cmd_module.CommandBot(cfg, cmd_module.TelegramAPI(cfg.telegram_token), max_hours=0)

    def _stop(*_):
        log.info("Останавливаюсь…")
        STOP.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # «Проснулся» — один раз в 6 часов максимум, чтобы при перезапусках
    # (Restart=always) не было спама, но было видно, что сервер жив.
    try:
        import time as _t
        last_ping = float(store.get_meta("worker_ping_at", 0) or 0)
        if os.environ.get("WORKER_STARTUP_PING", "1") == "1" and _t.time() - last_ping > 6 * 3600:
            chats = cfg.telegram_chat_ids
            text = (f"✅ <b>Бот запущен на сервере</b>\n"
                    f"🕒 {dipscanner.now_msk():%d.%m.%Y %H:%M} МСК\n"
                    f"📊 Портфель: {cfg.portfolio_title}\n"
                    f"⏱ Проверка сделок каждые {cfg.poll_interval // 60} мин\n"
                    f"<i>Сделки придут сюда сразу после публикации автором</i>")
            for cid in chats:
                try:
                    listener.api.send(cid, text)
                except Exception as e:  # noqa: BLE001
                    log.warning("стартовое сообщение не отправлено: %s", e)
            store.set_meta("worker_ping_at", _t.time())
            store.flush()
    except Exception as e:  # noqa: BLE001
        log.warning("стартовое сообщение: %s", e)

    threads = [threading.Thread(target=listener.run, name="tg-commands", daemon=True)]
    if os.environ.get("WORKER_CHECK", "1") == "1":
        threads.append(threading.Thread(target=portfolio_loop, args=(cfg, store),
                                        name="portfolio", daemon=True))
    for t in threads:
        t.start()
    log.info("Готов: команды в Telegram и проверка портфеля работают постоянно")

    try:
        while not STOP.is_set():
            STOP.wait(5)
    finally:
        try:
            store.flush()
            store.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("Остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
