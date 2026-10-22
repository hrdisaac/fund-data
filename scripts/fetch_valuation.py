#!/usr/bin/env python3
"""
Fetch 蛋卷基金's index valuation table (PE/PB, 10-year percentiles, ROE, dividend yield)
and save a normalised copy that the fund viewer can read from GitHub.

Runs once a day in GitHub Actions. Uses only the Python standard library.

Output (in the repository's data/ folder):
  index_valuation.json      normalised data the app reads (stable field names)
  index_valuation_raw.json  the source's original reply, for troubleshooting

If the fetch fails or the data looks wrong, the script exits with an error and
leaves the previous good files untouched (GitHub then emails you about the failed run).

Local test without network:  python fetch_valuation.py --from-file sample.json
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

URLS = [
    "https://danjuanfunds.com/djapi/index_eva/dj",
    "https://danjuanapp.com/djapi/index_eva/dj",
]
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://danjuanfunds.com/djmodule/value-center",
}
ATTEMPTS_PER_URL = 3
MIN_ITEMS = 5
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
BJ = timezone(timedelta(hours=8))


def log(msg):
    print(msg, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def get_items(payload):
    """The table is normally at data.items; accept a few other shapes too."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data, list):
            return data
        if isinstance(payload.get("items"), list):
            return payload["items"]
    if isinstance(payload, list):
        return payload
    return None


def first(item, *keys):
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return None


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def to_date(v, today=None):
    """Dates come as millisecond timestamps, 'YYYY-MM-DD', or 'MM-DD' (no year: take the latest such day ≤ today)."""
    if v is None:
        return None
    f = num(v)
    if f is not None and f > 1e11:                       # milliseconds since 1970
        return datetime.fromtimestamp(f / 1000, BJ).date().isoformat()
    s = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):
        return s[:10]
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", s)
    if m and today is not None:
        mo, d = int(m.group(1)), int(m.group(2))
        year = today.year if (mo, d) <= (today.month, today.day) else today.year - 1
        return f"{year}-{mo:02d}-{d:02d}"
    return s


def flag(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return False


def normalise(items, today=None):
    raw_rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ts_date = to_date(first(it, "ts"), today)
        txt_date = to_date(first(it, "date"), today)
        raw_rows.append({
            "code": first(it, "index_code", "code", "symbol"),
            "name": first(it, "name", "index_name"),
            "pe": num(first(it, "pe", "pe_ttm")),
            "pb": num(first(it, "pb")),
            "pe_pct": num(first(it, "pe_percentile", "pe_over_history")),
            "pb_pct": num(first(it, "pb_percentile", "pb_over_history")),
            "roe": num(first(it, "roe")),
            "dividend_yield": num(first(it, "yeild", "yield", "dividend_yield")),
            "eva_type": first(it, "eva_type"),
            # prefer a full date; 'ts' is a timestamp, 'date' is often only MM-DD
            "date": ts_date if ts_date and len(ts_date) == 10 and ts_date[4] == "-" else txt_date,
            "date_text": first(it, "date"),
            "type": first(it, "ttype"),
            "history_from": to_date(first(it, "begin_at"), today),
            "pb_flag": flag(first(it, "pb_flag")),
            "peg": num(first(it, "peg")),
            "bond_yield_raw": num(first(it, "bond_yeild", "bond_yield")),
            "url": first(it, "url"),
        })

    # Percentiles / ROE / yield may be fractions (0.5548) or percents (55.48): decide per column.
    def scale(col):
        vals = [r[col] for r in raw_rows if r[col] is not None]
        is_fraction = bool(vals) and max(abs(v) for v in vals) <= 1.0
        for r in raw_rows:
            if r[col] is not None:
                r[col] = round(r[col] * 100, 2) if is_fraction else round(r[col], 2)
        return "fraction→percent" if is_fraction else "already percent"

    scaling = {col: scale(col) for col in ("pe_pct", "pb_pct", "roe", "dividend_yield")}
    for r in raw_rows:
        for col in ("pe", "pb"):
            if r[col] is not None:
                r[col] = round(r[col], 3)
    rows = [r for r in raw_rows if r["code"] and r["name"]]
    return rows, scaling


def validate(rows):
    if len(rows) < MIN_ITEMS:
        return f"only {len(rows)} usable indices (expected at least {MIN_ITEMS})"
    with_pe = sum(1 for r in rows if r["pe"] is not None)
    if with_pe < 0.8 * len(rows):
        return f"only {with_pe}/{len(rows)} indices have a PE value"
    bad_pct = [r["code"] for r in rows for c in ("pe_pct", "pb_pct")
               if r[c] is not None and not (0 <= r[c] <= 100)]
    if bad_pct:
        return f"percentiles outside 0–100 for {bad_pct[:5]}"
    return None


def load_payload(from_file):
    if from_file:
        with open(from_file, encoding="utf-8") as f:
            return json.load(f), from_file
    last_error = None
    for url in URLS:
        for attempt in range(1, ATTEMPTS_PER_URL + 1):
            try:
                status, body = fetch(url)
                log(f"{url} attempt {attempt}: HTTP {status}, {len(body)} bytes")
                try:
                    return json.loads(body), url
                except json.JSONDecodeError:
                    last_error = f"not JSON (starts with: {body[:200]!r})"
                    log(f"  {last_error}")
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                log(f"{url} attempt {attempt}: {last_error}")
            except Exception as e:                       # network errors, timeouts
                last_error = f"{type(e).__name__}: {e}"
                log(f"{url} attempt {attempt}: {last_error}")
            time.sleep(5 * attempt)
    log(f"All attempts failed. Last error: {last_error}")
    return None, None


def main():
    from_file = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--from-file" else None
    payload, source = load_payload(from_file)
    if payload is None:
        sys.exit(1)
    items = get_items(payload)
    if items is None:
        keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        log(f"Could not find the index table in the reply. Top-level keys: {keys}")
        sys.exit(1)

    rows, scaling = normalise(items, datetime.now(BJ).date())
    problem = validate(rows)
    fields_seen = sorted({k for it in items if isinstance(it, dict) for k in it})
    log(f"Fields in the source data: {fields_seen}")
    log(f"Scaling decisions: {scaling}")
    if problem:
        log(f"Data looks wrong, keeping the previous files: {problem}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    out = {
        "source": "蛋卷基金 index_eva (data from Wind) — personal use",
        "source_url": source,
        "fetched_at_utc": now.isoformat(timespec="seconds"),
        "fetched_at_beijing": now.astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S"),
        "percent_fields_are": "percent (0–100)",
        "schema": 2,
        "count": len(rows),
        "fields_seen": fields_seen,
        "items": sorted(rows, key=lambda r: r["code"]),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    norm_path = os.path.join(OUT_DIR, "index_valuation.json")
    raw_path = os.path.join(OUT_DIR, "index_valuation_raw.json")
    try:
        with open(norm_path, encoding="utf-8") as f:
            unchanged = json.load(f).get("items") == out["items"]
    except (OSError, json.JSONDecodeError):
        unchanged = False
    if unchanged:
        log(f"Valuation data unchanged ({len(rows)} indices); nothing to update.")
    else:
        with open(norm_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        log(f"Saved {len(rows)} indices to data/index_valuation.json")

    log("First 5 indices:")
    for r in out["items"][:5]:
        log(f"  {r['code']} {r['name']}: PE {r['pe']} ({r['pe_pct']}%), PB {r['pb']} ({r['pb_pct']}%), "
            f"ROE {r['roe']}, 股息率 {r['dividend_yield']}, {r['date']}, type {r['type']!r}, "
            f"history from {r['history_from']}, pb_flag {r['pb_flag']}")


if __name__ == "__main__":
    main()
