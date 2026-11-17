"""JSON-хранилище состояния — вариант для запуска в GitHub Actions.

В отличие от SQLite, состояние здесь — обычный текстовый файл, который удобно
коммитить в репозиторий: он маленький, читаемый в диффе и не ломается при
одновременных запусках (workflow сериализуются через concurrency).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

MAX_SEEN = 5000        # сколько последних id сделок помним
MAX_ASSETS = 400       # сколько карточек бумаг кэшируем


class JsonStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.data: dict[str, Any] = {"seen": {}, "meta": {}, "assets": {}}
        self.dirty = False
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data["seen"] = dict(loaded.get("seen") or {})
                    self.data["meta"] = dict(loaded.get("meta") or {})
                    self.data["assets"] = dict(loaded.get("assets") or {})
            except Exception:
                pass

    # --- seen -----------------------------------------------------------
    def is_seen(self, tx_id: str) -> bool:
        return tx_id in self.data["seen"]

    def mark_seen(self, tx_ids: list[str]) -> None:
        now = int(time.time())
        added = 0
        for t in tx_ids:
            if t and t not in self.data["seen"]:
                self.data["seen"][t] = now
                added += 1
        if added:
            self.dirty = True

    def seen_count(self) -> int:
        return len(self.data["seen"])

    def prune(self, keep_days: int = 120) -> None:
        edge = int(time.time()) - keep_days * 86400
        seen = self.data["seen"]
        keep = {k: v for k, v in seen.items() if v >= edge}
        if len(keep) > MAX_SEEN:
            newest = sorted(keep.items(), key=lambda kv: -kv[1])[:MAX_SEEN]
            keep = dict(newest)
        if len(keep) != len(seen):
            self.data["seen"] = keep
            self.dirty = True
        assets = self.data["assets"]
        if len(assets) > MAX_ASSETS:
            ordered = sorted(assets.items(), key=lambda kv: -(kv[1].get("updated_at") or 0))[:MAX_ASSETS]
            self.data["assets"] = dict(ordered)
            self.dirty = True

    # --- meta -----------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        value = self.data["meta"].get(key, default)
        return value if value is not None else default

    def set_meta(self, key: str, value: Any) -> None:
        if self.data["meta"].get(key) != value:
            self.data["meta"][key] = value
            self.dirty = True

    # --- кэш карточек ----------------------------------------------------
    def get_asset(self, ticker: str, ttl: int = 86400) -> dict | None:
        item = self.data["assets"].get(ticker)
        if not item:
            return None
        if time.time() - (item.get("updated_at") or 0) > ttl:
            return None
        return item.get("payload")

    def put_asset(self, ticker: str, payload: dict) -> None:
        self.data["assets"][ticker] = {"payload": payload, "updated_at": int(time.time())}
        self.dirty = True

    # --- сохранение ------------------------------------------------------
    def save(self) -> bool:
        """Атомарная запись. Возвращает True, если файл реально изменился."""
        if not self.dirty:
            return False
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.dirty = False
        return True

    def close(self) -> None:
        self.save()
