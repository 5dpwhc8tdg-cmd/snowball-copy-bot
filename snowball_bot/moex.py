"""MOEX ISS: официальные признаки выпуска (без токена и регистрации).

Что даёт:
  * ISQUALIFIEDINVESTORS — «Бумаги для квалифицированных инвесторов» (0/1)
  * HASDEFAULT / HASTECHNICALDEFAULT — зафиксирован дефолт / технический дефолт
  * LISTLEVEL — уровень листинга (1/2/3)
  * тип купона, дата погашения, номинал

Чего НЕ даёт: кредитный рейтинг. Его берём отдельно (см. ratings.py),
потому что именно рейтинг определяет, нужен ли неквалу тест у брокера.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

ISS = "https://iss.moex.com/iss"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; snowball-copy-bot/1.0)",
    "Accept": "application/json",
}

_FIELDS = {
    "SHORTNAME": "shortname",
    "NAME": "name",
    "ISQUALIFIEDINVESTORS": "is_qualified_only",
    "HASDEFAULT": "has_default",
    "HASTECHNICALDEFAULT": "has_tech_default",
    "LISTLEVEL": "list_level",
    "BOND_TYPE": "bond_type",
    "BOND_SUBTYPE": "bond_subtype",
    "COUPON_DETAILS": "coupon_details",
    "FACEVALUE": "facevalue",
    "MATDATE": "matdate",
    "GROUPNAME": "group",
}


def _get(url: str, timeout: int = 30, retries: int = 3) -> dict | None:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    log.debug("MOEX: не удалось получить %s: %s", url, last)
    return None


def _bool(value) -> bool | None:
    if value is None:
        return None
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def fetch_security(isin: str) -> dict | None:
    """Признаки выпуска с MOEX. Возвращает dict или None, если бумага не найдена."""
    data = _get(f"{ISS}/securities/{isin}.json?iss.meta=off")
    if not data or "description" not in data:
        return None
    rows = data["description"].get("data") or []
    raw = {}
    for row in rows:
        if len(row) >= 3:
            raw[row[0]] = row[2]
    if not raw.get("SECID"):
        return None
    out: dict = {}
    for moex_key, our_key in _FIELDS.items():
        out[our_key] = raw.get(moex_key)
    for key in ("is_qualified_only", "has_default", "has_tech_default"):
        out[key] = _bool(out.get(key))
    if out.get("list_level") is not None:
        try:
            out["list_level"] = int(out["list_level"])
        except (TypeError, ValueError):
            pass
    out["isin"] = isin
    return out


def summary_for(isin: str, store=None, ttl: int = 7 * 86400) -> dict | None:
    """С кэшем в хранилище (карточки MOEX меняются редко)."""
    if store is not None:
        cached = store.get_asset(f"moex:{isin}", ttl=ttl)
        if cached:
            return cached
    data = fetch_security(isin)
    if data and store is not None:
        store.put_asset(f"moex:{isin}", data)
    return data
