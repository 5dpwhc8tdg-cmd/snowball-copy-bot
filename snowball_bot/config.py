"""Конфигурация бота: всё берётся из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORTFOLIO = "ukbRjaXjfg"
DEFAULT_PORTFOLIO_TITLE = "Облигационные фантазии"


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный загрузчик .env (без внешних зависимостей)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)).replace(",", ".") or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return (_env(key, "1" if default else "0").lower() in {"1", "true", "yes", "y", "on"})


def _env_list(key: str) -> list[str]:
    raw = _env(key, "")
    if not raw:
        return []
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


@dataclass
class Config:
    # --- что отслеживаем -------------------------------------------------
    portfolio_id: str = field(default_factory=lambda: _env("PORTFOLIO_ID", DEFAULT_PORTFOLIO))
    portfolio_title: str = field(default_factory=lambda: _env("PORTFOLIO_TITLE", DEFAULT_PORTFOLIO_TITLE))

    # --- как часто опрашивать -------------------------------------------
    poll_interval: int = field(default_factory=lambda: max(60, _env_int("POLL_INTERVAL", 300)))
    page_size: int = field(default_factory=lambda: min(200, max(10, _env_int("PAGE_SIZE", 100))))

    # --- что присылать ---------------------------------------------------
    only_type: str = field(default_factory=lambda: _env("ONLY_TYPE", "both").lower())  # buy|sell|both
    min_amount: float = field(default_factory=lambda: _env_float("MIN_AMOUNT", 0))
    ignore_tickers: list[str] = field(default_factory=lambda: [t.upper() for t in _env_list("IGNORE_TICKERS")])
    include_income: bool = field(default_factory=lambda: _env_bool("INCLUDE_INCOME", False))
    copy_scale: float = field(default_factory=lambda: _env_float("COPY_SCALE", 0))  # 0 = не пересчитывать
    asset_details: bool = field(default_factory=lambda: _env_bool("ASSET_DETAILS", True))

    # первое включение: "baseline" — только запомнить историю, "last:N" — прислать N последних сделок
    first_run: str = field(default_factory=lambda: _env("FIRST_RUN", "baseline").lower())

    # --- куда присылать ---------------------------------------------------
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_ids: list[str] = field(default_factory=lambda: _env_list("TELEGRAM_CHAT_ID"))
    telegram_commands: bool = field(default_factory=lambda: _env_bool("TELEGRAM_COMMANDS", True))

    max_token: str = field(default_factory=lambda: _env("MAX_BOT_TOKEN"))
    max_user_id: str = field(default_factory=lambda: _env("MAX_USER_ID"))
    max_chat_id: str = field(default_factory=lambda: _env("MAX_CHAT_ID"))
    max_api_base: str = field(default_factory=lambda: _env("MAX_API_BASE", "https://platform-api2.max.ru"))

    console: bool = field(default_factory=lambda: _env_bool("CONSOLE_OUTPUT", False))

    # --- где хранить состояние -------------------------------------------
    # sqlite (VPS/домашний ПК) или json (GitHub Actions: файл коммитится в репозиторий)
    state_backend: str = field(default_factory=lambda: _env("STATE_BACKEND", "sqlite").lower())
    state_path: str = field(default_factory=lambda: _env("STATE_PATH", "state.json"))

    # --- прочее -----------------------------------------------------------
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/bot.sqlite3"))
    log_path: str = field(default_factory=lambda: _env("LOG_PATH", "data/bot.log"))
    tz: str = field(default_factory=lambda: _env("TZ_NAME", "Europe/Moscow"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", False))

    @property
    def snowball_url(self) -> str:
        return f"https://snowball-income.com/public/portfolios/{self.portfolio_id}"

    def channels(self) -> list[str]:
        ch = []
        if self.telegram_token and self.telegram_chat_ids:
            ch.append("telegram")
        if self.max_token and (self.max_user_id or self.max_chat_id):
            ch.append("max")
        if self.console or not ch:
            ch.append("console")
        return ch
