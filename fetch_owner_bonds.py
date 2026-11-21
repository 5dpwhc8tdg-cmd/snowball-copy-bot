"""Качает дневную историю по всем бумагам, которые покупал владелец, и индекс RGBI."""
import json
import sys
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor, as_completed

from history_fetch import fetch_bond_history, fetch_index

FRM, TILL = "2023-06-01", "2026-11-21"

if __name__ == "__main__":
    tx = json.loads(Path("data/hist/portfolio_tx.json").read_text())
    tickers = sorted({t["ticker"] for t in tx if t.get("type") == 1 and t.get("assetType") == 2})
    print(f"бумаг: {len(tickers)}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_bond_history, tk, FRM, TILL): tk for tk in tickers}
        for f in as_completed(futs):
            done += 1
            tk = futs[f]
            try:
                rows = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! {tk}: {e}", flush=True)
                continue
            if done % 40 == 0 or done == len(tickers):
                print(f"  {done}/{len(tickers)} … ({tk}: {len(rows)} дней)", flush=True)
    rows = fetch_index("RGBI", FRM, TILL)
    print(f"RGBI: {len(rows)} дней")
