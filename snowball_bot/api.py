"""Клиент публичных данных Snowball Income (без авторизации).

Работает через тот же публичный API, который использует сайт:
  GET https://snowball-income.com/extapi/api/public/transactions?portfolioId=...&...
Обязательные заголовки: X-Instance, Accept-Language, x-user-locale, x-device-type.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

BASE = "https://snowball-income.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru",
    "X-Instance": "ru",
    "x-user-locale": "ru-RU",
    "x-device-type": "desktop",
    "Referer": "https://snowball-income.com/",
}

# Типы операций в API Snowball
TX_BUY = 1
TX_SELL = 2
TX_EXCHANGE = 4
TX_INCOME = 5  # купоны / дивиденды / прочие начисления


class SnowballError(RuntimeError):
    pass


def _http_get_json(url: str, timeout: int = 30, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            last = SnowballError(f"HTTP {e.code} для {url} {body}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise last
        except Exception as e:  # таймауты, обрывы соединения
            last = e
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
    raise SnowballError(str(last))


def fetch_transactions(
    portfolio_id: str,
    page: int = 1,
    page_size: int = 100,
    types: tuple[int, ...] = (TX_BUY, TX_SELL, TX_EXCHANGE),
) -> dict:
    """Последние сделки портфеля (по убыванию даты)."""
    params = [
        ("page", str(page)),
        ("pageSize", str(page_size)),
        ("sortBy", "date"),
        ("sortDirection", "desc"),
        ("filter", ""),
        ("portfolioId", portfolio_id),
    ]
    for t in types:
        params.append(("supportedTypes[]", str(t)))
    url = f"{BASE}/extapi/api/public/transactions?" + urllib.parse.urlencode(params)
    return _http_get_json(url)


def fetch_all_transactions(portfolio_id: str, max_pages: int = 5, page_size: int = 100) -> list[dict]:
    """Несколько страниц подряд — на случай, если за один опрос накопилось много сделок."""
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        data = fetch_transactions(portfolio_id, page=page, page_size=page_size)
        rows = data.get("data") or []
        new = 0
        for r in rows:
            tid = r.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            out.append(r)
            new += 1
        total = data.get("totalCount") or 0
        if not rows or new == 0 or page * page_size >= total:
            break
    return out


# ---------------------------------------------------------------------------
# Детали бумаги (цена, номинал, доходность, рейтинг) — нужны только для
# «умных» подсказок в уведомлении. Ходят через _next/data, где в пути лежит
# buildId, который меняется при каждом релизе сайта. Поэтому buildId
# определяется автоматически и кэшируется.
# ---------------------------------------------------------------------------

def get_build_id() -> str | None:
    """buildId Next.js: нужен в пути /_next/data/<buildId>/... Он меняется при релизах сайта."""
    import re

    try:
        req = urllib.request.Request(f"{BASE}/public/portfolios", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        log.debug("buildId: не удалось получить страницу: %s", e)
        return None

    # 1) самый надёжный источник — ссылка на _buildManifest.js
    m = re.search(r"/_next/static/([^/\"']+)/_buildManifest\.js", html)
    if m and m.group(1) not in {"css", "chunks", "media"}:
        return m.group(1)
    # 2) сам __NEXT_DATA__
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1)
    return None


def fetch_asset(ticker: str, exchange: str = "MCX", currency: str = "RUB", build_id: str | None = None) -> dict | None:
    """Карточка бумаги. Возвращает dict или None, если не получилось."""
    bid = build_id or get_build_id()
    if not bid:
        return None
    slug = f"{ticker}.{exchange}.{currency}"
    url = f"{BASE}/_next/data/{bid}/public/asset/{slug}.json?slug={slug}"
    try:
        data = _http_get_json(url, retries=1)
    except Exception as e:
        log.debug("asset %s: %s", ticker, e)
        return None
    asset = (data.get("pageProps") or {}).get("asset")
    if not asset:
        return None
    return asset


def asset_summary(asset: dict) -> dict:
    """Выжимка из карточки бумаги: то, что реально нужно в уведомлении."""
    ratings = asset.get("bondRatings") or {}
    rating = None
    for key in ("expertRA", "acra", "nra", "nkr"):
        val = (ratings.get(key) or {}).get("displayRating")
        if val and val != "-":
            rating = val
            break
    nominal = asset.get("nominal")
    price = asset.get("currentPrice")
    pct = round(price / nominal * 100, 1) if price and nominal else None
    return {
        "name": asset.get("description"),
        "nominal": nominal,
        "price": price,
        "price_pct_of_nominal": pct,
        "ytm": asset.get("yieldToMaturity"),
        "maturity": (asset.get("maturityDate") or "")[:10] or None,
        "rating": rating,
        "is_qualified_only": asset.get("isQualifiedOnly") or asset.get("forQualifiedInvestors"),
    }
