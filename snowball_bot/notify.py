"""Отправка уведомлений: Telegram, MAX, консоль."""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Notifier:
    name: str

    def send(self, text_html: str, plain: str | None = None) -> bool:  # pragma: no cover
        raise NotImplementedError


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 20) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": "snowball-copy-bot/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:500]
    except Exception as e:
        return 0, str(e)


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_ids: list[str]):
        super().__init__("telegram")
        self.token = token
        self.chat_ids = chat_ids

    def send(self, text_html: str, plain: str | None = None) -> bool:
        ok = True
        for chat_id in self.chat_ids:
            ok &= self._send_one(chat_id, text_html)
        return ok

    def _send_one(self, chat_id: str, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, 4):
            status, body = _post_json(url, payload)
            if status == 200:
                return True
            log.warning("Telegram: HTTP %s %s", status, body)
            if status == 429:
                time.sleep(5 * attempt)
                continue
            if status in (400, 403, 404):  # неверный chat_id/токен — ретраить бессмысленно
                return False
            time.sleep(2 * attempt)
        return False


class MaxNotifier(Notifier):
    """MAX (max.ru). Токен бота: business.max.ru → Чат-боты → Настройки."""

    def __init__(self, token: str, user_id: str = "", chat_id: str = "", api_base: str = "https://platform-api2.max.ru"):
        super().__init__("max")
        self.token = token
        self.user_id = user_id
        self.chat_id = chat_id
        self.api_base = api_base.rstrip("/")

    def send(self, text_html: str, plain: str | None = None) -> bool:
        text = (plain or text_html)[:4000]
        return self._send_one(text)

    def _send_one(self, text: str) -> bool:
        params = {}
        if self.chat_id:
            params["chat_id"] = self.chat_id
        elif self.user_id:
            params["user_id"] = self.user_id
        if not params:
            log.error("MAX: не задан ни MAX_USER_ID, ни MAX_CHAT_ID")
            return False
        url = f"{self.api_base}/messages?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self.token}
        for attempt in range(1, 4):
            status, body = _post_json(url, {"text": text}, headers=headers)
            if status in (200, 201):
                return True
            log.warning("MAX: HTTP %s %s", status, body)
            if status == 401:
                log.error("MAX: токен не принят (401). Проверьте MAX_BOT_TOKEN.")
                return False
            time.sleep(2 * attempt)
        return False

    def get_me(self) -> dict | None:
        """Проверка токена: GET /me."""
        url = f"{self.api_base}/me"
        req = urllib.request.Request(url, headers={"Authorization": self.token, "User-Agent": "snowball-copy-bot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log.warning("MAX /me: %s", e)
            return None


class ConsoleNotifier(Notifier):
    def __init__(self):
        super().__init__("console")

    def send(self, text_html: str, plain: str | None = None) -> bool:
        print("\n" + "─" * 60)
        print((plain or text_html))
        print("─" * 60)
        return True


def build_notifiers(cfg) -> list[Notifier]:
    out: list[Notifier] = []
    if cfg.telegram_token and cfg.telegram_chat_ids:
        out.append(TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_ids))
    if cfg.max_token and (cfg.max_user_id or cfg.max_chat_id):
        out.append(MaxNotifier(cfg.max_token, cfg.max_user_id, cfg.max_chat_id, cfg.max_api_base))
    if cfg.console or not out:
        out.append(ConsoleNotifier())
    return out
