"""Кредитный рейтинг выпуска — то, что определяет, нужен ли неквалу тест.

Источник: публичная таблица облигаций smart-lab.ru (там же, где рейтинг видят
инвесторы). Берём только те выпуски, у которых рейтинг A+ и выше: именно они
по указанию ЦБ доступны неквалу БЕЗ тестирования. Всё, чего нет в списке,
считаем «тест, скорее всего, потребуется» (это оценка, а не гарантия).

Экономика запросов: одна страница таблицы = 100 бумаг. Обходим страницы,
пока не наткнёмся на рейтинги ниже A+ — это ~9 страниц (~900 выпусков).
Всё кэшируется в state.json, поэтому полный обход случается редко.
"""
from __future__ import annotations

import gzip
import io
import logging
import re
import time
import urllib.request

log = logging.getLogger(__name__)

BASE = "https://smart-lab.ru/q/bonds/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# От лучшего к худшему
ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
         "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"]
SAFE_UP_TO = "A+"          # без теста доступны рейтинги A+ и выше
MAX_PAGES = 12             # предохранитель: ~1200 выпусков


def rank(rating: str | None) -> int:
    """0 = AAA (лучший), ORDER.index('A+') = порог неквала, len(ORDER) = неизвестно."""
    if not rating:
        return len(ORDER)
    r = rating.strip().upper().replace(" ", "")
    m = re.match(r"^(?:RU|RUS)?(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)", r)
    if not m:
        return len(ORDER)
    return ORDER.index(m.group(1)) if m.group(1) in ORDER else len(ORDER)


def is_no_test_needed(rating: str | None) -> bool:
    return rank(rating) <= ORDER.index(SAFE_UP_TO)


def _fetch(url: str, timeout: int = 40, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.9",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2 * attempt)
    log.warning("Рейтинги: не удалось загрузить %s: %s", url, last)
    return ""


def _parse_page(html: str) -> list[tuple[str, str, str]]:
    """Возвращает [(isin, name, rating)] со страницы."""
    start = html.find('<table class="_hidden">')
    if start < 0:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html[start:], re.S)
    out: list[tuple[str, str, str]] = []
    for row in rows[1:]:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)]
        m = re.search(r"/q/bonds/(RU[0-9A-Z]{10})/", row)
        if not m or len(cells) < 8:
            continue
        out.append((m.group(1), cells[1], cells[7]))
    return out


def fetch_rating_map(max_pages: int = MAX_PAGES) -> dict[str, str]:
    """ISIN → рейтинг для всех выпусков с рейтингом A+ и выше."""
    result: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        sort_path = "order_by_sm_rating/desc/"
        url = f"{BASE}{sort_path}" if page == 1 else f"{BASE}{sort_path}page{page}/"
        html = _fetch(url)
        rows = _parse_page(html)
        if not rows:
            log.info("Рейтинги: страница %d пуста, останавливаюсь", page)
            break
        for isin, _name, rating in rows:
            if is_no_test_needed(rating):
                result[isin] = rating
        worst = max((r for _, _, r in rows), key=rank)
        if rank(worst) > ORDER.index(SAFE_UP_TO):
            log.info("Рейтинги: страница %d дошла до %s — дальше рейтинги ниже порога", page, worst)
            break
        time.sleep(0.5)
    log.info("Рейтинги: собрано %d выпусков с рейтингом ≥ %s", len(result), SAFE_UP_TO)
    return result


# ---------------------------------------------------------------------------
# Хранилище карты рейтингов (кэш на сутки)
# ---------------------------------------------------------------------------
CACHE_KEY = "ratings:map"


def get_rating_map(store, force: bool = False, ttl: int = 86400 * 2) -> dict[str, str]:
    if store is not None and not force:
        cached = store.get_asset(CACHE_KEY, ttl=ttl)
        if cached and isinstance(cached.get("map"), dict):
            return cached["map"]
    data = fetch_rating_map()
    if store is not None and data:
        store.put_asset(CACHE_KEY, {"map": data})
    return data


def lookup(rating_map: dict[str, str], isin: str) -> str | None:
    return rating_map.get(isin)
