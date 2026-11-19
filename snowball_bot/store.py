"""Хранилище состояния: какие сделки уже видели, кэш карточек бумаг, mute."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    tx_id     TEXT PRIMARY KEY,
    seen_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS asset_cache (
    ticker     TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_at_idx ON seen(seen_at);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- seen -----------------------------------------------------------
    def is_seen(self, tx_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE tx_id = ?", (tx_id,))
        return cur.fetchone() is not None

    def mark_seen(self, tx_ids: list[str]) -> None:
        now = int(time.time())
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen(tx_id, seen_at) VALUES(?, ?)",
            [(t, now) for t in tx_ids],
        )
        self.conn.commit()

    def seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def prune(self, keep_days: int = 90) -> None:
        edge = int(time.time()) - keep_days * 86400
        self.conn.execute("DELETE FROM seen WHERE seen_at < ?", (edge,))
        self.conn.commit()

    # --- meta -----------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.conn.commit()

    # --- кэш карточек ----------------------------------------------------
    def get_asset(self, ticker: str, ttl: int = 86400) -> dict | None:
        row = self.conn.execute(
            "SELECT payload, updated_at FROM asset_cache WHERE ticker = ?", (ticker,)
        ).fetchone()
        if not row:
            return None
        if time.time() - row[1] > ttl:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def put_asset(self, ticker: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO asset_cache(ticker, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
            (ticker, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        self.conn.commit()

    def flush(self) -> None:  # noqa: D401
        """По умолчанию ничего не делаем — переопределяют те, кому нужно."""

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def open_store(cfg):
    """Выбор бэкенда хранения: STATE_BACKEND=json (для GitHub Actions) или sqlite."""
    backend = (getattr(cfg, "state_backend", "sqlite") or "sqlite").lower()
    if backend == "json":
        from .json_store import JsonStore

        return JsonStore(cfg.state_path)
    return Store(cfg.db_path)
