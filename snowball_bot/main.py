"""Основной цикл бота: опрос публичного портфеля Snowball → уведомления."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import traceback
import urllib.request
import json

from . import analytics, api, formatter, moex as moex_api, ratings as ratings_api, sizing
from .config import Config, load_dotenv
from .notify import Notifier, build_notifiers
from .store import Store, open_store

log = logging.getLogger("snowball_bot")

STOP = threading.Event()


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
def setup_logging(cfg: Config) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(cfg.log_path)) or ".", exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(cfg.log_path, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# Обогащение данными о бумаге
# ---------------------------------------------------------------------------
def enrich_moex(cfg: Config, store, txs: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """MOEX-признаки (квал/дефолт/листинг) + рейтинги (нужен ли тест) по тикерам."""
    if not cfg.asset_details:
        return {}, {}
    moex_map: dict[str, dict] = {}
    rating_map: dict[str, str] = {}
    tickers = [t.get("ticker") or "" for t in txs]
    for ticker in dict.fromkeys(t for t in tickers if t):
        data = moex_api.summary_for(ticker, store=store)
        if data:
            moex_map[ticker] = data
        time.sleep(0.15)
    if cfg.check_qualified:
        try:
            full = ratings_api.get_rating_map(store) if store is not None else ratings_api.fetch_rating_map()
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось получить карту рейтингов: %s", e)
            full = {}
        for ticker in moex_map:
            if ticker in full:
                rating_map[ticker] = full[ticker]
    return moex_map, rating_map


def market_context(tx: dict) -> dict:
    """Где владелец вошёл относительно дневного диапазона и что было с ценой потом."""
    if tx.get("type") != api.TX_BUY:
        return {}
    try:
        day = (tx.get("date") or "")[:10]
        rows = analytics._block(
            f"{analytics.ISS}/engines/stock/markets/bonds/securities/{tx.get('ticker')}/candles.json"
            f"?iss.meta=off&interval=10&from={day}&till={day}", "candles")
    except Exception:  # noqa: BLE001
        return {}
    rows = [r for r in rows if r.get("close")]
    if len(rows) < 3:
        return {}
    paid = tx.get("pricePercent")
    if not paid:
        return {}
    high = max(r["high"] for r in rows if r.get("high"))
    low = min(r["low"] for r in rows if r.get("low"))
    if high <= low:
        return {}
    pos = (paid - low) / (high - low) * 100
    eod = rows[-1]["close"]
    return {
        "price_in_day_range_pct": pos,
        "eod_after_pct": (eod - paid) / paid * 100,
        "day_high": high,
        "day_low": low,
    }


def enrich(cfg: Config, store: Store, tickers: list[str], exchange_hint: dict[str, str]) -> dict[str, dict]:
    """Возвращает {ticker: краткая карточка} — с кэшем в БД."""
    out: dict[str, dict] = {}
    if not cfg.asset_details:
        return out
    build_id = store.get_meta("build_id")
    need_refresh = False
    for ticker in dict.fromkeys(tickers):
        cached = store.get_asset(ticker, ttl=7 * 86400)
        if cached:
            out[ticker] = cached
            continue
        asset = api.fetch_asset(ticker, exchange=exchange_hint.get(ticker, "MCX"), build_id=build_id)
        if asset is None:
            # возможно устарел buildId — попробуем обновить один раз
            if not need_refresh:
                need_refresh = True
                new_bid = api.get_build_id()
                if new_bid and new_bid != build_id:
                    build_id = new_bid
                    store.set_meta("build_id", build_id)
                    asset = api.fetch_asset(ticker, exchange=exchange_hint.get(ticker, "MCX"), build_id=build_id)
            if asset is None:
                continue
        summary = api.asset_summary(asset)
        store.put_asset(ticker, summary)
        out[ticker] = summary
        time.sleep(0.2)  # вежливость к сайту
    return out


# ---------------------------------------------------------------------------
# Фильтрация
# ---------------------------------------------------------------------------
def apply_filters(cfg: Config, txs: list[dict]) -> list[dict]:
    out = []
    for tx in txs:
        kind = formatter.classify(tx)
        if kind == formatter.OTHER and not cfg.include_income:
            continue
        if cfg.only_type == "buy" and kind != formatter.BUY:
            continue
        if cfg.only_type == "sell" and kind != formatter.SELL:
            continue
        if (tx.get("ticker") or "").upper() in cfg.ignore_tickers:
            continue
        amount = tx.get("totalPrice") or 0
        if cfg.min_amount and amount < cfg.min_amount:
            continue
        out.append(tx)
    return out


def sort_chrono(txs: list[dict]) -> list[dict]:
    return sorted(txs, key=lambda t: (t.get("date") or "", t.get("id") or ""))


# ---------------------------------------------------------------------------
# Один цикл проверки
# ---------------------------------------------------------------------------
def check_once(cfg: Config, store: Store, notifiers: list[Notifier], dry_run: bool = False) -> int:
    types = (api.TX_BUY, api.TX_SELL, api.TX_EXCHANGE)
    if cfg.include_income:
        types = types + (api.TX_INCOME,)
    rows = api.fetch_all_transactions(cfg.portfolio_id, max_pages=5, page_size=cfg.page_size)
    if cfg.include_income:
        extra = api.fetch_transactions(cfg.portfolio_id, page=1, page_size=cfg.page_size, types=(api.TX_INCOME,))
        known = {r.get("id") for r in rows}
        rows += [r for r in (extra.get("data") or []) if r.get("id") not in known]

    if not rows:
        log.info("Сделок не получено (портфель пуст или API изменился)")
        return 0

    initialized = bool(store.get_meta("initialized"))
    all_ids = [r.get("id") for r in rows if r.get("id")]
    new_rows = [r for r in rows if not store.is_seen(r.get("id") or "")]

    if cfg.force_last_n:
        # Ручной тестовый прогон из GitHub Actions: прислать последние N сделок,
        # даже если они уже отмечены как просмотренные.
        new_rows = sort_chrono(rows[: cfg.force_last_n])
        store.mark_seen(all_ids)
        store.set_meta("initialized", True)
        log.info("Тестовый прогон: принудительно отправляю %d последних сделок", len(new_rows))
    elif not initialized:
        # Самый первый запуск: запоминаем историю, чтобы не выстрелить 4000 пушами.
        mode = cfg.first_run
        n = 0
        if mode.startswith("last:"):
            try:
                n = int(mode.split(":", 1)[1])
            except ValueError:
                n = 0
        to_send = sort_chrono(new_rows[:n]) if n else []
        store.mark_seen(all_ids)
        store.set_meta("initialized", True)
        log.info("Первый запуск: запомнил %d сделок как историю, к отправке %d", len(rows), len(to_send))
        new_rows = to_send

    if not new_rows:
        log.info("Новых сделок нет (всего проверено %d)", len(rows))
        return 0

    new_rows = sort_chrono(new_rows)
    filtered = apply_filters(cfg, new_rows)
    store.mark_seen([r.get("id") for r in new_rows if r.get("id")])

    if not filtered:
        log.info("Новых сделок %d, но все отфильтрованы настройками", len(new_rows))
        return 0

    exchange_hint = {(t.get("ticker") or ""): (t.get("exchange") or "MCX") for t in filtered}
    infos = enrich(cfg, store, list(exchange_hint.keys()), exchange_hint)
    moex_map, rating_map = enrich_moex(cfg, store, filtered)

    sizes = {}
    for tx in filtered:
        price_unit = tx.get("price") or 0
        sizes[tx.get("id")] = sizing.compute(
            his_qty=tx.get("amount") or 0,
            price_per_unit=price_unit,
            nkd_per_unit=tx.get("nkd") or 0,
            my_capital=cfg.my_capital,
            his_capital=cfg.owner_capital,
            max_position_pct=cfg.my_max_position_pct,
        )

    profiles: dict[str, dict] = {}
    market_map: dict[str, dict] = {}
    if cfg.asset_details:
        for tx in filtered:
            ticker = tx.get("ticker") or ""
            if ticker in profiles:
                continue
            isin = (moex_map.get(ticker) or {}).get("isin") or ticker
            try:
                profiles[ticker] = analytics.full_profile(isin, store=store)
            except Exception as e:  # noqa: BLE001
                log.debug("Профиль %s: %s", ticker, e)
            try:
                market_map[ticker] = market_context(tx)
            except Exception:  # noqa: BLE001
                pass

    if cfg.only_no_test:
        keep = []
        for tx in filtered:
            ticker = tx.get("ticker") or ""
            is_sell = formatter.classify(tx) == formatter.SELL
            rating_ok = formatter.rating_rank(rating_map.get(ticker)) <= formatter.RATING_ORDER.index("A+")
            if is_sell or rating_ok:
                keep.append(tx)
        if len(keep) != len(filtered):
            log.info("ONLY_NO_TEST: отброшено %d сделок, недоступных неквалу", len(filtered) - len(keep))
        filtered = keep
        if not filtered:
            return 0

    html_text = formatter.format_batch(
        filtered,
        tz=cfg.tz,
        portfolio_title=cfg.portfolio_title,
        portfolio_url=cfg.snowball_url,
        copy_scale=(cfg.my_capital or cfg.copy_scale),
        infos=infos,
        moex_map=moex_map,
        rating_map=rating_map,
        profiles=profiles,
        market_map=market_map,
        sizes=sizes,
    )
    plain_text = formatter.to_plain(html_text)

    if dry_run:
        print(plain_text)
        print()
        return len(filtered)

    for chunk in formatter.split_for_limit(html_text):
        sent = False
        for n in notifiers:
            try:
                sent |= n.send(chunk, formatter.to_plain(chunk))
            except Exception as e:
                log.error("Отправка через %s не удалась: %s", n.name, e)
        if not sent:
            log.error("Не удалось отправить уведомление ни в один канал")
    log.info("Отправлено уведомление о %d сделках", len(filtered))
    return len(filtered)


# ---------------------------------------------------------------------------
# Команды в Telegram (/status, /last, /mute ...)
# ---------------------------------------------------------------------------
class TelegramCommands(threading.Thread):
    def __init__(self, cfg: Config, store: Store):
        super().__init__(daemon=True, name="tg-commands")
        self.cfg = cfg
        self.store = store
        self.offset = store.get_meta("tg_offset", 0) or 0

    # --- helpers -------------------------------------------------------
    def _api(self, method: str, params: dict) -> dict:
        import urllib.parse

        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/{method}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "snowball-copy-bot/1.0"})
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _reply(self, chat_id: str, text: str) -> None:
        from .notify import TelegramNotifier

        TelegramNotifier(self.cfg.telegram_token, [str(chat_id)]).send(text)

    def _allowed(self, chat_id: str) -> bool:
        return str(chat_id) in {str(c) for c in self.cfg.telegram_chat_ids}

    # --- main ----------------------------------------------------------
    def run(self) -> None:
        log.info("Слушаю команды в Telegram")
        while not STOP.is_set():
            try:
                data = self._api("getUpdates", {"offset": self.offset, "timeout": 30})
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    self.store.set_meta("tg_offset", self.offset)
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    text = (msg.get("text") or "").strip()
                    chat_id = str((msg.get("chat") or {}).get("id") or "")
                    if not text or not chat_id or not self._allowed(chat_id):
                        continue
                    self.handle(chat_id, text)
            except Exception as e:
                log.debug("Команды: %s", e)
                time.sleep(5)

    def handle(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("start", "help"):
            self._reply(
                chat_id,
                "🤖 <b>Snowball Copy Bot</b>\n\n"
                f"Портфель: <b>{self.cfg.portfolio_title}</b>\n"
                f"Опрос каждые {self.cfg.poll_interval // 60} мин.\n\n"
                "/status — состояние\n"
                "/last — 5 последних сделок\n"
                "/mute — заглушить на 2 часа\n"
                "/unmute — включить уведомления",
            )
        elif cmd == "status":
            muted = bool((self.store.get_meta("mute_until", 0) or 0) > time.time())
            self._reply(
                chat_id,
                "📟 <b>Статус</b>\n"
                f"Сделок в базе: {self.store.seen_count()}\n"
                f"Последняя проверка: {self.store.get_meta('last_check_human', '—')}\n"
                f"Уведомления: {'на паузе' if muted else 'включены'}\n"
                f"Фильтр: {self.cfg.only_type}, мин. сумма {self.cfg.min_amount:g} ₽",
            )
        elif cmd in ("last", "last5"):
            try:
                rows = api.fetch_all_transactions(self.cfg.portfolio_id, max_pages=1, page_size=5)
                rows = sort_chrono(apply_filters(self.cfg, rows))[-5:]
                if not rows:
                    self._reply(chat_id, "Сделок не найдено.")
                    return
                infos = enrich(
                    self.cfg,
                    self.store,
                    [(r.get("ticker") or "") for r in rows],
                    {(r.get("ticker") or ""): (r.get("exchange") or "MCX") for r in rows},
                )
                moex_map, rating_map = enrich_moex(self.cfg, self.store, rows)
                html_text = formatter.format_batch(
                    rows,
                    tz=self.cfg.tz,
                    portfolio_title=self.cfg.portfolio_title,
                    portfolio_url=self.cfg.snowball_url,
                    copy_scale=self.cfg.copy_scale,
                    infos=infos,
                    moex_map=moex_map,
                    rating_map=rating_map,
                )
                for chunk in formatter.split_for_limit(html_text):
                    self._reply(chat_id, chunk)
            except Exception as e:
                self._reply(chat_id, f"Не смог получить сделки: {e}")
        elif cmd == "mute":
            self.store.set_meta("mute_until", int(time.time()) + 7200)
            self._reply(chat_id, "🔇 Уведомления на паузе 2 часа.")
        elif cmd == "unmute":
            self.store.set_meta("mute_until", 0)
            self._reply(chat_id, "🔔 Уведомления включены.")
        else:
            self._reply(chat_id, "Не понимаю команду. /help")


# ---------------------------------------------------------------------------
# Цикл
# ---------------------------------------------------------------------------
def loop(cfg: Config, store: Store, notifiers: list[Notifier]) -> None:
    failures = 0
    while not STOP.is_set():
        try:
            if (store.get_meta("mute_until", 0) or 0) > time.time():
                log.info("Уведомления на паузе — проверяю молча")
                silence = True
            else:
                silence = False
            if silence:
                rows = api.fetch_all_transactions(cfg.portfolio_id, max_pages=2, page_size=cfg.page_size)
                store.mark_seen([r.get("id") for r in rows if r.get("id")])
            else:
                check_once(cfg, store, notifiers)
            store.set_meta("last_check_human", time.strftime("%d.%m.%Y %H:%M:%S"))
            failures = 0
        except Exception as e:
            failures += 1
            log.error("Ошибка проверки (%d): %s", failures, e)
            if cfg.debug:
                log.error(traceback.format_exc())
            if failures in (5, 20):
                for n in notifiers:
                    try:
                        n.send(f"⚠️ Бот не может получить данные портфеля: {e}", f"⚠️ Бот не может получить данные: {e}")
                    except Exception:
                        pass
            STOP.wait(min(60 * failures, 900))
            continue
        store.prune(keep_days=120)
        STOP.wait(cfg.poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Бот уведомлений о сделках публичного портфеля Snowball Income")
    parser.add_argument("--once", action="store_true", help="одна проверка и выход")
    parser.add_argument("--dry-run", action="store_true", help="показать сообщение в консоли, не отправлять")
    parser.add_argument("--test-notify", action="store_true", help="проверить каналы доставки")
    parser.add_argument("--commands", action="store_true",
                        help="слушать команды в Telegram (/now, /dips, /data, /cap …)")
    parser.add_argument("--env", default=".env", help="путь к .env")
    sub = parser.add_subparsers(dest="cmd")
    p_dips = sub.add_parser("dips", help="сканировать резкие падения облигаций на MOEX")
    p_dips.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    cfg = Config()
    setup_logging(cfg)

    log.info("Портфель: %s (%s)", cfg.portfolio_title, cfg.portfolio_id)
    log.info("Каналы: %s", ", ".join(cfg.channels()))
    log.info("Интервал опроса: %s сек", cfg.poll_interval)

    if args.commands:
        from pathlib import Path

        import runpy

        return int(runpy.run_path(str(Path(__file__).resolve().parent.parent / "commands.py"),
                                  run_name="__main__") and 0)

    if getattr(args, "cmd", None) == "dips":
        from . import dips_cmd

        return dips_cmd.run(cfg, dry_run=getattr(args, "dry_run", False))

    store = open_store(cfg)
    notifiers = build_notifiers(cfg)

    if args.test_notify:
        text = (
            "✅ <b>Проверка связи</b>\n"
            f"Бот готов следить за портфелем «{cfg.portfolio_title}».\n"
            f"Опрос каждые {cfg.poll_interval // 60} мин."
        )
        for n in notifiers:
            ok = n.send(text, formatter.to_plain(text))
            print(f"{n.name}: {'OK' if ok else 'ОШИБКА'}")
        return 0

    if args.once or args.dry_run:
        try:
            check_once(cfg, store, notifiers, dry_run=args.dry_run)
        finally:
            store.close()  # у JsonStore это запись файла состояния
        return 0

    def _stop(*_):
        log.info("Останавливаюсь…")
        STOP.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if cfg.telegram_commands and cfg.telegram_token and cfg.telegram_chat_ids:
        TelegramCommands(cfg, store).start()

    # первый проход сразу при старте
    try:
        loop(cfg, store, notifiers)
    finally:
        store.close()
    log.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
