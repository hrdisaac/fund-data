#!/usr/bin/env python3
"""
Market indicators for the fund viewer (standard library only; runs daily in GitHub Actions).

  1) CSI index PE percentiles  → data/csi_valuation.json
     Indices listed in indices.txt (plus 000906 中证800, always). Source: 中证指数官网
     index-perf (its "peg" field is the rolling PE, 滚动市盈率; there is no PB). One request
     per index fetches ~10 years of daily data, so nothing has to be stored between runs.

  2) 股债性价比 / equity risk premium  → data/market.json  ("erp")
     ERP = 100 / PE(中证800) − China 10-year government bond yield (both in %).
     Bond yields: Eastmoney RPTA_WEB_TREASURYYIELD, field EMM00166466.

  4) 恐惧贪婪指数 / fear-and-greed  → data/market.json  ("fear_greed")
     Six transparent components, each scored 0–100 as its percentile within its own last 500
     trading days (no look-ahead; volatility inverted), averaged with equal weights:
       成交活跃度 (中证全指 5-day average turnover vs its 60-day average), 量能方向 (share of the last
       10 days' turnover traded on rising days), 避险需求 (20-day return of 沪深300 minus a bond index),
       杠杆率 (margin trading as % of turnover, one day late), 动量 (沪深300 vs its 125-day average),
       波动率 (20-day realised volatility of 沪深300, annualised; high = fear),
       期货升贴水 (annualised basis of the main 沪深300 index future IF vs the index; CFFEX daily
       statistics, history kept in data/history/futures/IF.json).
     "live" carries what the app needs to recompute the components during the trading day.

  3) 低估行业个数 / number of undervalued industries  → data/market.json  ("industries")
     Eastmoney RPT_VALUEINDUSTRY_DET (东方财富行业板块, daily PE_TTM / PB_MRQ since 2017).
     That table is ~300,000 rows, so its history is kept in the repository in monthly files
     (data/history/industry/YYYY-MM.json). The first runs backfill it board by board within a
     time budget; after that each run adds the latest day(s) with one or two requests.

Every part is independent: if one source fails, the others are still saved, and the failed
part keeps its previous answer (marked with an error). The script never deletes good data.

Local test without network:  python fetch_market.py --from-dir samples/   (see test_fetch_market.py)
"""
import bisect
import io
import math
import zipfile
import collections
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INDICES_FILE = os.path.join(ROOT, "indices.txt")
DATA_DIR = os.path.join(ROOT, "data")
CSI_OUT = os.path.join(DATA_DIR, "csi_valuation.json")
MARKET_OUT = os.path.join(DATA_DIR, "market.json")
IND_DIR = os.path.join(DATA_DIR, "history", "industry")
BJ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ERP_INDEX = "000906"                 # 中证800 (the default, published as "erp")
ERP_OTHERS = [("000300", "沪深300"), ("000905", "中证500"), ("000852", "中证1000"), ("000985", "中证全指")]
ERP_HISTORY_YEARS = 20               # requested history for ERP indices (CSI returns what it has)
ERP_MIN_WINDOW_YEARS = 3             # a past day gets a temperature only with ≥3 years of history before it
FWD_DAYS = 365                       # "then one year": first trading day ≥ 365 calendar days later
MAX_INDICES = 80
YEARS = 10
MIN_YEARS_FOR_PCT = 1.0              # shorter history: no percentile at all
SHORT_HISTORY_YEARS = 3.0            # shorter than this: percentile is shown but flagged
IND_MIN_YEARS = 3.0                  # a board needs 3 years of history before it is counted
UNDER, OVER = 30.0, 70.0             # percentile thresholds (our stated rule)
TIME_BUDGET = float(os.environ.get("MARKET_TIME_BUDGET", 11 * 60))
PAUSE = float(os.environ.get("MARKET_PAUSE", 0.8))
BACKFILL_PAGE_SIZES = (5000, 500)    # try one big page per board; fall back if the source caps it
MAX_CONSECUTIVE_FAILURES = 6
FG_WINDOW, FG_MIN = 500, 250          # fear-and-greed: percentile window / minimum history (trading days)
FG_TURNOVER = ["000985", "000906"]    # 中证全指 (all A shares); 中证800 as a fallback
FG_STOCK = "000300"
FG_BONDS = ["H11001", "H11006", "000012"]   # 中证全债, 中证国债, 上证国债 — first one that works
MARGIN_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_MARGIN_DAILYTRADE"
              "&columns=ALL&pageSize=500&pageNumber={page}&sortColumns=STATISTICS_DATE&sortTypes=-1")

CSI_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           "?indexCode={code}&startDate={start}&endDate={end}")
CSI_HEADERS = {"Referer": "https://www.csindex.com.cn/", "Accept": "application/json"}
EM_HEADERS = {"Referer": "https://data.eastmoney.com/"}
TREASURY_URLS = [
    "https://datacenter.eastmoney.com/api/data/get?type=RPTA_WEB_TREASURYYIELD&sty=ALL"
    "&st=SOLAR_DATE&sr=-1&p={page}&ps=500",
    "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_TREASURYYIELD"
    "&columns=ALL&sortColumns=SOLAR_DATE&sortTypes=-1&pageNumber={page}&pageSize=500",
]
Y10_FIELD = "EMM00166466"
IND_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEINDUSTRY_DET"
           "&columns=ALL&pageSize={size}&pageNumber={page}&sortColumns=TRADE_DATE&sortTypes={order}{filter}")

STARTED = time.monotonic()
SAMPLE_DIR = None                     # set by --from-dir: answer requests from local files


def log(msg):
    print(msg, flush=True)


def time_left():
    return TIME_BUDGET - (time.monotonic() - STARTED)


# ---------------------------------------------------------------- HTTP

def _sample_lookup(url):
    """--from-dir: the request URL is matched against samples/index.json {substring: file}."""
    with open(os.path.join(SAMPLE_DIR, "index.json"), encoding="utf-8") as f:
        table = json.load(f)
    best = None
    for key, name in table.items():
        if key in url and (best is None or len(key) > len(best[0])):
            best = (key, name)
    if best is None:
        raise urllib.error.HTTPError(url, 404, "no sample", None, None)
    if best[1] is None:
        raise urllib.error.URLError("simulated network error")
    with open(os.path.join(SAMPLE_DIR, best[1]), encoding="utf-8") as f:
        return json.load(f)


def get_json(url, headers, attempts=2, timeout=40):
    if SAMPLE_DIR:
        return _sample_lookup(url)
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers)
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 403, 404):
                break
        except Exception as e:                       # timeouts, resets, bad JSON
            last = e
        if i + 1 < attempts:
            time.sleep(3 * (i + 1))         # wait before a retry, not after the last attempt
    raise last


def pause():
    if not SAMPLE_DIR:
        time.sleep(PAUSE)


# ---------------------------------------------------------------- helpers

def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def ymd(s):
    """'20261104' / '2026-11-04 00:00:00' → '2026-11-04' (or None)."""
    s = str(s or "").strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def d_of(s):
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def years_before(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:                               # 29 Feb
        return day.replace(year=day.year - years, day=28)


def pct_below(sorted_vals, v):
    """Share of values strictly below v, in percent (0 = cheapest ever, ~100 = dearest)."""
    if not sorted_vals:
        return None
    return round(100.0 * bisect.bisect_left(sorted_vals, v) / len(sorted_vals), 2)


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def weekly(rows, key=lambda r: r[0]):
    """Keep the last row of every ISO week (rows sorted by date; date string is key(row))."""
    out, last_week = [], None
    for r in rows:
        y, w, _ = d_of(key(r)).isocalendar()
        if (y, w) == last_week:
            out[-1] = r
        else:
            out.append(r)
            last_week = (y, w)
    return out


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_if_changed(path, obj, ignore=("fetched_at_beijing",)):
    """Write compact JSON unless only the ignored keys changed. Returns True if written."""
    old = read_json(path)
    strip = lambda o: {k: v for k, v in o.items() if k not in ignore} if isinstance(o, dict) else o
    if old is not None and strip(old) == strip(json.loads(json.dumps(obj))):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return True


def em_result(j):
    """Eastmoney data-centre replies: {"result": {"pages", "count", "data": [...]}} (or result=null)."""
    res = (j or {}).get("result") if isinstance(j, dict) else None
    if not res:
        msg = (j or {}).get("message") if isinstance(j, dict) else None
        raise RuntimeError(f"no result ({msg or 'empty reply'})")
    return res.get("data") or [], int(res.get("pages") or 1)


def em_count(j):
    try:
        return int(j["result"]["count"])
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 1) CSI index PE

def read_index_codes(path):
    codes, seen = [ERP_INDEX], {ERP_INDEX}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                for tok in re.split(r"[\s,;，；]+", line.split("#", 1)[0]):
                    tok = tok.strip().upper()
                    if re.fullmatch(r"\d{6}|H\d{5}", tok) and tok not in seen:
                        seen.add(tok)
                        codes.append(tok)
    return codes[:MAX_INDICES]


_CSI_CACHE = {}


def fetch_csi(code, today, years=YEARS):
    """One request per index; a longer history fetched earlier in the run is reused."""
    hit = _CSI_CACHE.get(code)
    if hit and hit[0] >= years:
        return hit[1]
    res = _fetch_csi(code, today, years)
    _CSI_CACHE[code] = (years, res)
    return res


def _fetch_csi(code, today, years):
    start = years_before(today, years) - timedelta(days=20)
    url = CSI_URL.format(code=code, start=start.strftime("%Y%m%d"), end=today.strftime("%Y%m%d"))
    j = get_json(url, CSI_HEADERS)
    rows = j.get("data") if isinstance(j, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected reply (keys {list(j)[:6] if isinstance(j, dict) else type(j).__name__})")
    out, name, full, amounts = {}, "", "", {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = ymd(r.get("tradeDate"))
        if not d:
            continue
        name = r.get("indexNameCn") or name
        full = r.get("indexNameCnAll") or full
        out[d] = (num(r.get("close")), num(r.get("peg")))
        a = num(r.get("tradingValue"))
        if a is not None and a > 0:
            amounts[d] = a
    return name, full, sorted((d, c, pe) for d, (c, pe) in out.items()), amounts


def csi_item(code, name, full, rows):
    """rows: [(date, close, pe)] sorted. Returns the published item or raises."""
    pe_rows = [(d, pe) for d, _, pe in rows if pe is not None and pe > 0]
    if not pe_rows:
        raise RuntimeError("no PE values (the index may not be published by CSI, or has no PE)")
    last_d, last_pe = pe_rows[-1]
    latest = d_of(last_d)
    w10 = years_before(latest, YEARS).isoformat()
    w5 = years_before(latest, 5).isoformat()
    in10 = [(d, pe) for d, pe in pe_rows if d > w10]
    # Coverage is measured inside the window: CSI sometimes has an isolated PE value before a
    # multi-year gap (seen for 990001: one value in 2016, then nothing until 2020-02-27).
    first_d = in10[0][0]
    years = round((latest - d_of(first_d)).days / 365.25, 1)
    v10 = sorted(pe for _, pe in in10)
    v5 = sorted(pe for d, pe in pe_rows if d > w5)
    close = next((c for d, c, _ in reversed(rows) if d == last_d), None)
    ok = years >= MIN_YEARS_FOR_PCT
    return {
        "code": code, "name": (name or code)[:24], "name_full": (full or name or code)[:40],
        "pe": round(last_pe, 2), "pe_date": last_d, "close": close,
        "pct10y": pct_below(v10, last_pe) if ok else None,
        "pct5y": pct_below(v5, last_pe) if ok and years >= 5 else None,
        "years": years, "history_from": first_d, "window_from": in10[0][0],
        "full_window": years >= YEARS - 0.2, "short_history": years < SHORT_HISTORY_YEARS,
        "n": len(v10),
        "pe_min": round(v10[0], 2), "pe_median": round(quantile(v10, 0.5), 2), "pe_max": round(v10[-1], 2),
        "pe_p30": round(quantile(v10, 0.3), 2), "pe_p70": round(quantile(v10, 0.7), 2),
        "series": [[int(d.replace("-", "")), round(pe, 2)] for d, pe in weekly(in10)],
    }


def run_csi(today, now_text):
    codes = read_index_codes(INDICES_FILE)
    previous = {i["code"]: i for i in (read_json(CSI_OUT, {}) or {}).get("items", []) if isinstance(i, dict)}
    items, errors, raw = {}, {}, {}
    consecutive = 0
    log(f"CSI: {len(codes)} indices ({', '.join(codes)})")
    for code in codes:
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            errors[code] = "skipped: too many failures in a row"
        else:
            try:
                name, full, rows, _ = fetch_csi(code, today)
                raw[code] = rows
                items[code] = csi_item(code, name, full, rows)
                consecutive = 0
                it = items[code]
                log(f"  {code} {it['name_full']}: PE {it['pe']} on {it['pe_date']}, "
                    f"{it['pct10y']}% (10y window from {it['window_from']}, {it['n']} days; history {it['years']} y)")
            except Exception as e:
                consecutive += 1 if not isinstance(e, RuntimeError) else 0
                errors[code] = f"{type(e).__name__}: {e}"[:200]
                log(f"  {code}: FAILED {errors[code]}" + ("; keeping previous answer" if code in previous else ""))
            pause()
        if code in errors and code in previous:
            items[code] = dict(previous[code], kept_from_previous_run=True)
    out = {"schema": 1, "source": "中证指数有限公司官网 index-perf（滚动市盈率）— personal use",
           "fetched_at_beijing": now_text, "percent_fields_are": "percent (0–100); share of days in the window with a lower PE",
           "count": len(items), "items": [items[c] for c in codes if c in items], "errors": errors}
    if not items:
        log("CSI: nothing usable; keeping the previous file.")
        return raw, False
    log("CSI: " + ("saved data/csi_valuation.json" if write_if_changed(CSI_OUT, out) else "unchanged"))
    return raw, True


# ---------------------------------------------------------------- 2) ERP

def fetch_treasury(since):
    """{date: 10y yield %} back to `since` (newest first, page by page)."""
    last_err = None
    for tmpl in TREASURY_URLS:
        y10, seen_first = {}, set()
        try:
            page, pages = 1, 1
            while page <= min(pages, 40):
                rows, pages = em_result(get_json(tmpl.format(page=page), EM_HEADERS))
                if not rows:
                    break
                first = json.dumps(rows[0], sort_keys=True)
                if first in seen_first:
                    raise RuntimeError("paging parameter ignored (same page twice)")
                seen_first.add(first)
                oldest = None
                for r in rows:
                    d = ymd(r.get("SOLAR_DATE"))
                    v = num(r.get(Y10_FIELD))
                    if d and v is not None and 0 < v < 20:
                        y10[d] = v
                    if d:
                        oldest = d if oldest is None or d < oldest else oldest
                if oldest is not None and oldest < since:
                    break
                page += 1
                pause()
            if len(y10) < 200:
                raise RuntimeError(f"only {len(y10)} yields with field {Y10_FIELD}")
            return y10
        except Exception as e:
            last_err = e
            log(f"  treasury source failed ({tmpl.split('?')[0]}): {type(e).__name__}: {e}")
    raise last_err


def temp_band(temp):
    return ("极低温" if temp < 10 else "较低温" if temp < 30 else "适中" if temp < 70
            else "较高温" if temp < 90 else "极高温")


BANDS = ["极低温", "较低温", "适中", "较高温", "极高温"]


def fwd_stats(vals):
    if not vals:
        return None
    v = sorted(vals)
    return {"n": len(v), "median": round(100 * quantile(v, 0.5), 2), "mean": round(100 * sum(v) / len(v), 2),
            "up": round(100 * sum(1 for x in v if x > 0) / len(v), 1),
            "p25": round(100 * quantile(v, 0.25), 2), "p75": round(100 * quantile(v, 0.75), 2),
            "min": round(100 * v[0], 2), "max": round(100 * v[-1], 2)}


def erp_block(csi_rows, y10, code=ERP_INDEX, name="中证800"):
    """csi_rows: [(date, close, pe)]. Returns the published block (latest values, levels, weekly series,
    and the 'same temperature band → return over the next year' statistic)."""
    ydates = sorted(y10)
    series = []
    for d, close, pe in csi_rows:
        if pe is None or pe <= 0:
            continue
        i = bisect.bisect_right(ydates, d) - 1
        if i < 0 or (d_of(d) - d_of(ydates[i])).days > 7:
            continue
        y = y10[ydates[i]]
        series.append((d, close, pe, y, 100.0 / pe - y))
    if len(series) < 250:
        raise RuntimeError(f"only {len(series)} days with both PE and bond yield")
    d, close, pe, y, erp = series[-1]
    latest = d_of(d)
    w10 = years_before(latest, YEARS).isoformat()
    w5 = years_before(latest, 5).isoformat()
    v10 = sorted(s[4] for s in series if s[0] > w10)
    v5 = sorted(s[4] for s in series if s[0] > w5)
    pct10 = pct_below(v10, erp)
    temp = round(100 - pct10)
    q = lambda p: round(quantile(v10, p), 3)
    # temperature of every past day, from the 10 years up to that day only (no look-ahead)
    first = d_of(series[0][0])
    win, dq, temps = [], collections.deque(), {}
    for sd, _, _, _, e in series:
        cur = d_of(sd)
        start = years_before(cur, YEARS).isoformat()
        while dq and dq[0][0] <= start:
            del win[bisect.bisect_left(win, dq.popleft()[1])]
        dq.append((sd, e))
        bisect.insort(win, e)
        if (cur - first).days / 365.25 >= ERP_MIN_WINDOW_YEARS:
            temps[sd] = round(100 - pct_below(win, e))
    # the price change over the next year, grouped by the band on the starting day
    cdates = [s[0] for s in series if s[1]]
    closes = {s[0]: s[1] for s in series if s[1]}
    by_band, everything, starts = collections.defaultdict(list), [], collections.defaultdict(list)
    for sd, t in temps.items():
        target = (d_of(sd) + timedelta(days=FWD_DAYS)).isoformat()
        j = bisect.bisect_left(cdates, target)
        if j >= len(cdates) or sd not in closes or (d_of(cdates[j]) - d_of(target)).days > 10:
            continue
        r = closes[cdates[j]] / closes[sd] - 1
        b = temp_band(t)
        by_band[b].append(r)
        starts[b].append(sd)
        everything.append(r)
    used = sorted(x for b in starts for x in starts[b])
    forward = None
    if everything:
        forward = {"horizon_days": FWD_DAYS, "from": used[0], "to": used[-1], "temps_from": min(temps),
                   "all": fwd_stats(everything),
                   "bands": [dict(fwd_stats(by_band[b]) or {"n": 0}, band=b,
                                  years=len({x[:4] for x in starts[b]}),
                                  first=min(starts[b]) if starts[b] else None, last=max(starts[b]) if starts[b] else None)
                             for b in BANDS]}
    return {
        "index": code, "index_name": name, "date": d, "pe": round(pe, 2), "y10": round(y, 4),
        "y10_date": ydates[bisect.bisect_right(ydates, d) - 1],
        "erp": round(erp, 3), "ratio": round((100.0 / pe) / y, 3),
        "pct10y": pct10, "pct5y": pct_below(v5, erp), "temperature": temp, "band": temp_band(temp),
        "window_from": next(s[0] for s in series if s[0] > w10), "n": len(v10), "history_from": series[0][0],
        # ERP values at these percentiles of the window: temperature 90 / 70 / 50 / 30 / 10
        "levels": {"p10": q(0.1), "p30": q(0.3), "p50": q(0.5), "p70": q(0.7), "p90": q(0.9)},
        "forward": forward,
        "series": [[int(s[0].replace("-", "")), round(s[4], 3), s[1]] for s in weekly([s for s in series if s[0] > w10])],
    }


def run_erp(today):
    """Default block (中证800) plus the other indices; each index is independent."""
    since = (years_before(today, ERP_HISTORY_YEARS) - timedelta(days=30)).isoformat()
    y10 = fetch_treasury(since)
    log(f"ERP: {len(y10)} bond-yield days from {min(y10)}, latest {max(y10)} = {y10[max(y10)]}")
    blocks, errors = {}, {}
    for code, name in [(ERP_INDEX, "中证800")] + ERP_OTHERS:
        try:
            _, _, rows, _ = fetch_csi(code, today, ERP_HISTORY_YEARS)
            pause()
            if not rows:
                raise RuntimeError("no CSI data")
            b = erp_block(rows, y10, code, name)
            if (d_of(b["date"]) - d_of(b["y10_date"])).days > 5:
                raise RuntimeError(f"bond yield is stale ({b['y10_date']} vs PE {b['date']})")
            blocks[code] = b
            f = b["forward"]
            cur = next((x for x in f["bands"] if x["band"] == b["band"]), None) if f else None
            log(f"ERP {name}: {b['date']} PE {b['pe']} → E/P {100 / b['pe']:.2f}% − 10y {b['y10']}% = {b['erp']}%, "
                f"{b['pct10y']}% percentile (10y), temperature {b['temperature']} ({b['band']}); history from {b['history_from']}"
                + (f"; next-year stats {f['from']}…{f['to']}, current band n={cur['n']} median {cur.get('median')}% "
                   f"up {cur.get('up')}% vs all median {f['all']['median']}%" if cur else "; no next-year stats yet"))
        except Exception as e:
            errors[code] = f"{type(e).__name__}: {e}"[:200]
            log(f"ERP {name} ({code}) failed: {errors[code]}")
    if ERP_INDEX not in blocks:
        raise RuntimeError(errors.get(ERP_INDEX, "中证800 failed"))
    return blocks, errors


# ---------------------------------------------------------------- 4) fear and greed

def fg_band(score):
    return ("极度恐惧" if score <= 25 else "恐惧" if score < 45 else "中立" if score <= 55
            else "贪婪" if score < 75 else "极度贪婪")


def fetch_margin():
    """{date: 两融交易额占A股成交额 %} — all pages, sorted locally (does not rely on the sort parameter)."""
    out, seen, page, pages = {}, set(), 1, 1
    while page <= min(pages, 15):
        rows, pages = em_result(get_json(MARGIN_URL.format(page=page), EM_HEADERS))
        if not rows:
            break
        key = json.dumps(rows[0], sort_keys=True)
        if key in seen:
            raise RuntimeError("paging parameter ignored (same page twice)")
        seen.add(key)
        for r in rows:
            d, v = ymd(r.get("STATISTICS_DATE")), num(r.get("TRADE_AMT_RATIO"))
            if d and v is not None and 0 < v < 60 and (d not in out or str(r.get("DATE_MARK")) == "1"):
                out[d] = v
        page += 1
        pause()
    if len(out) < FG_MIN:
        raise RuntimeError(f"only {len(out)} days of margin data")
    return out


def rolling_scores(series, invert=False):
    """series [(date, value)] sorted → {date: percentile of value within the last FG_WINDOW values}
    (100 − percentile when invert)."""
    win, q, out = [], collections.deque(), {}
    for d, v in series:
        q.append(v)
        bisect.insort(win, v)
        if len(q) > FG_WINDOW:
            del win[bisect.bisect_left(win, q.popleft())]
        if len(q) >= FG_MIN:
            p = pct_below(win, v)
            out[d] = round(100 - p, 2) if invert else p
    return out


def fg_series(csi_close, amounts, bond_close, margin, all_close=None):
    """Raw component series: {key: [(date, value)]} (values in %)."""
    comps = {}
    ds = sorted(amounts)
    if len(ds) > 60:
        v = [amounts[d] for d in ds]
        comps["activity"] = [(ds[i], 100 * ((sum(v[i - 4:i + 1]) / 5) / (sum(v[i - 59:i + 1]) / 60) - 1)) for i in range(59, len(ds))]
    if all_close:
        dd = [d for d in ds if d in all_close]
        up = {dd[i]: all_close[dd[i]] > all_close[dd[i - 1]] for i in range(1, len(dd))}
        dd = dd[1:]
        comps["vol_direction"] = [(dd[i], 100 * sum(amounts[x] for x in dd[i - 9:i + 1] if up[x]) / sum(amounts[x] for x in dd[i - 9:i + 1]))
                                  for i in range(9, len(dd))]
    if bond_close:
        common = sorted(set(csi_close) & set(bond_close))
        comps["safe_haven"] = [(common[i], 100 * ((csi_close[common[i]] / csi_close[common[i - 20]])
                                                  - (bond_close[common[i]] / bond_close[common[i - 20]])))
                               for i in range(20, len(common))]
    if margin:
        comps["leverage"] = sorted(margin.items())
    ds = sorted(csi_close)
    if len(ds) > 125:
        c = [csi_close[d] for d in ds]
        run = sum(c[:125])
        mom = []
        for i in range(124, len(ds)):
            if i > 124:
                run += c[i] - c[i - 125]
            mom.append((ds[i], 100 * (c[i] / (run / 125) - 1)))
        comps["momentum"] = mom
        rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
        vol = []
        for i in range(19, len(rets)):
            w = rets[i - 19:i + 1]
            m = sum(w) / 20
            vol.append((ds[i + 1], 100 * math.sqrt(sum((x - m) ** 2 for x in w) / 19) * math.sqrt(250)))
        comps["volatility"] = vol
    return comps


FG_META = {
    "activity": ("成交活跃度", "%", "中证全指近 5 日平均成交额 ÷ 近 60 日平均 − 1"),
    "vol_direction": ("量能方向", "%", "近 10 个交易日里，上涨日的成交额占全部成交额的比例"),
    "safe_haven": ("避险需求", "%", "沪深300 近 20 日涨跌 − 债券指数近 20 日涨跌"),
    "leverage": ("杠杆率", "%", "两融交易额占 A 股成交额的比例（晚一天公布）"),
    "momentum": ("动量", "%", "沪深300 ÷ 近 125 日平均 − 1"),
    "volatility": ("波动率", "%", "沪深300 近 20 日收益率的年化标准差（越高越恐惧，得分反向）"),
}
FG_INVERT = {"volatility"}
FG_META["basis"] = ("期货升贴水", "%", "沪深300 股指期货主力合约相对指数的年化升贴水（贴水为负，越低越恐惧）")
FG_META["iv"] = ("隐含波动率", "%", "沪深300 股指期权 30 天隐含波动率：近月、次月平值期权按 Black-76 反推，按到期时间插值（越高越恐惧，得分反向）")
FG_INVERT.add("iv")

# ---- CFFEX: 沪深300 股指期货 (IF) daily statistics
CFFEX_HEADERS = {"Referer": "http://www.cffex.com.cn/"}
CFFEX_DAY = "http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/{ymd}_1.csv"
CFFEX_MONTH = "http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
IF_FILE_REL = os.path.join("history", "futures", "IF.json")
IF_HISTORY_YEARS = 4
IF_MIN_DTE = 5                     # contracts this close to expiry are not used (annualising explodes)
CFFEX_BUDGET = float(os.environ.get("MARKET_CFFEX_BUDGET", 180))   # seconds of CFFEX downloading per run
CFFEX_FILE_DEADLINE = 45           # whole-download limit per file (a slow trickle never trips the socket timeout)
CFFEX_MAX_FAILURES = 3             # in a row: stop for this run, the next run continues


def get_bytes(url, headers, attempts=2, timeout=20, deadline=CFFEX_FILE_DEADLINE):
    """Download with a limit on the WHOLE transfer (urlopen's timeout only limits each wait for data)."""
    if SAMPLE_DIR:
        return _sample_lookup(url)
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers)
    last = None
    for i in range(attempts):
        try:
            t0 = time.monotonic()
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                chunks = []
                while True:
                    b = r.read1(65536)          # read1: returns what has arrived; read() would wait for 64 KB or the end
                    if not b:
                        return b"".join(chunks)
                    chunks.append(b)
                    if time.monotonic() - t0 > deadline:
                        raise TimeoutError(f"download took longer than {deadline}s")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404):
                break
        except Exception as e:
            last = e
        if i + 1 < attempts:
            time.sleep(3 * (i + 1))         # wait before a retry, not after the last attempt
    raise last


def if_expiry(contract):
    """IF2612 → third Friday of 2026-12 (holiday shifts ignored: at most a few days)."""
    y, m = 2000 + int(contract[2:4]), int(contract[4:6])
    d = date(y, m, 1)
    first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + timedelta(days=14)


def parse_cffex_day(raw):
    """CFFEX daily CSV (GBK) → (IF rows [(contract, close, settle, oi)], IO rows [(yymm, 'C'|'P', strike, settle)])."""
    for enc in ("gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("gbk", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    head = [h.strip() for h in lines[0].split(",")]
    try:
        i_code, i_close, i_settle, i_oi = head.index("合约代码"), head.index("今收盘"), head.index("今结算"), head.index("持仓量")
    except ValueError:
        raise RuntimeError(f"unexpected CFFEX header {head[:8]}")
    out, opts = [], []
    for l in lines[1:]:
        f = [x.strip() for x in l.split(",")]
        if len(f) <= max(i_code, i_close, i_settle, i_oi):
            continue
        m = re.fullmatch(r"IO(\d{4})-([CP])-(\d+(?:\.\d+)?)", f[i_code])
        if m:
            settle = num(f[i_settle])
            if settle and settle > 0:
                opts.append((m.group(1), m.group(2), float(m.group(3)), settle))
            continue
        if not re.fullmatch(r"IF\d{4}", f[i_code]):
            continue
        close, settle, oi = num(f[i_close]), num(f[i_settle]), num(f[i_oi])
        if close and close > 0 and oi is not None:
            out.append((f[i_code], close, settle, int(oi)))
    return out, opts


def parse_cffex_csv(raw, day):
    return parse_cffex_day(raw)[0]


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black76(F, K, T, sigma, kind):
    """Undiscounted Black-76 price (rates are small and T short; the error is far below the rounding of prices)."""
    sd = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + sd * sd / 2) / sd
    d2 = d1 - sd
    return F * _ncdf(d1) - K * _ncdf(d2) if kind == "C" else K * _ncdf(-d2) - F * _ncdf(-d1)


def implied_vol(price, F, K, T, kind):
    intrinsic = max(0.0, F - K) if kind == "C" else max(0.0, K - F)
    if T <= 0 or price <= intrinsic + 1e-9 or price >= (F if kind == "C" else K):
        return None
    lo, hi = 0.005, 3.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if black76(F, K, T, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


IV_MIN_DTE = 7


def iv_30d(day, if_rows, opts):
    """30-day implied volatility in % from 沪深300 index options (IO), VIX-style:
    for the two nearest expiries with ≥ IV_MIN_DTE days, the forward comes from put-call parity at the strike
    where call and put are closest (F = K + C − P; this matches the options' own settlement prices, unlike the
    futures close), the at-the-money call and put give Black-76 IVs (averaged), and total variance is
    interpolated to 30 days.  if_rows is unused (kept for the call signature)."""
    d = d_of(day)
    terms = []
    for ym in sorted({o[0] for o in opts}):
        T_days = (if_expiry("IF" + ym) - d).days
        if T_days < IV_MIN_DTE:
            continue
        calls = {k: p for y, t, k, p in opts if y == ym and t == "C"}
        puts = {k: p for y, t, k, p in opts if y == ym and t == "P"}
        both = [k for k in calls if k in puts]
        if not both:
            continue
        k0 = min(both, key=lambda k: abs(calls[k] - puts[k]))
        F = k0 + calls[k0] - puts[k0]
        K = min(both, key=lambda k: abs(k - F))
        T = T_days / 365
        ivs = [v for v in (implied_vol(calls[K], F, K, T, "C"), implied_vol(puts[K], F, K, T, "P")) if v]
        if ivs:
            terms.append((T, sum(ivs) / len(ivs)))
        if len(terms) == 2:
            break
    return interp_30d(terms)


def interp_30d(terms):
    if not terms:
        return None
    T30 = 30 / 365
    if len(terms) == 1 or terms[0][0] >= T30:
        return round(100 * terms[0][1], 3)
    (T1, s1), (T2, s2) = terms
    var = (T1 * s1 * s1 * (T2 - T30) + T2 * s2 * s2 * (T30 - T1)) / (T2 - T1) / T30
    return round(100 * math.sqrt(max(var, 0)), 3)


def if_main(rows, day):
    """The main contract on a day: most open interest among contracts ≥ IF_MIN_DTE days from expiry."""
    d = d_of(day)
    ok = [r for r in rows if (if_expiry(r[0]) - d).days >= IF_MIN_DTE]
    return max(ok, key=lambda r: (r[3], -int(r[0][2:]))) if ok else None


def update_if_history(today):
    """Keeps {date: [contract, close, settle, oi]} for the main IF contract.
    Past months come from monthly zips, newest first (so ~1 year — enough for the component — arrives
    first), within CFFEX_BUDGET seconds per run; the current month from daily CSVs. Progress is saved
    after every file, so a run that is stopped early loses nothing. Returns (rows, fetched_count)."""
    path = os.path.join(DATA_DIR, IF_FILE_REL)
    st = read_json(path, {}) or {}
    if st.get("format") != 2:                      # format 2 adds the 30-day implied volatility to each day
        st = {}
    rows, months_done = dict(st.get("rows", {})), set(st.get("months_done", []))
    fetched, failures, t_start = 0, 0, time.monotonic()
    keep = years_before(today, IF_HISTORY_YEARS + 1).isoformat()

    def save():
        nonlocal rows
        rows = {k: v for k, v in sorted(rows.items()) if k >= keep}
        write_if_changed(path, {"note": "per day: main 沪深300 index future (IF) [contract, close, settlement, open interest] "
                                        "+ 30-day implied volatility (%) from 沪深300 index options (IO)",
                                "format": 2, "months_done": sorted(months_done), "rows": rows}, ignore=())

    def budget_left():
        return (time.monotonic() - t_start < CFFEX_BUDGET and failures < CFFEX_MAX_FAILURES
                and TIME_BUDGET - (time.monotonic() - STARTED) > 90)

    # current month first (daily files; usually one new file per run)
    d = today.replace(day=1)
    while d <= today and budget_left():
        day = d.isoformat()
        if d.weekday() < 5 and day not in rows:
            t0 = time.monotonic()
            try:
                raw = get_bytes(CFFEX_DAY.format(ym=d.strftime("%Y%m"), dd=d.strftime("%d"), ymd=d.strftime("%Y%m%d")), CFFEX_HEADERS)
                fr, op = parse_cffex_day(raw)
                main = if_main(fr, day)
                if main:
                    rows[day] = list(main) + [iv_30d(day, fr, op)]
                fetched += 1
                failures = 0
                log(f"  CFFEX {day}: {len(raw) // 1024} KB in {time.monotonic() - t0:.1f}s" + (f", main {main[0]}" if main else ""))
                save()
            except urllib.error.HTTPError as e:
                if e.code != 404:                       # 404: holiday, or today's file not out yet
                    failures += 1
                    log(f"  CFFEX {day} failed: HTTP {e.code}")
            except Exception as e:
                failures += 1
                log(f"  CFFEX {day} failed after {time.monotonic() - t0:.0f}s: {type(e).__name__}: {e}")
            pause()
        d += timedelta(days=1)

    # past months, newest first
    first = years_before(today, IF_HISTORY_YEARS).replace(day=1).strftime("%Y%m")
    m = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    todo = []
    while m.strftime("%Y%m") >= first:
        if m.strftime("%Y%m") not in months_done:
            todo.append(m.strftime("%Y%m"))
        m = (m - timedelta(days=1)).replace(day=1)
    for ym in todo:
        if not budget_left():
            break
        t0 = time.monotonic()
        try:
            raw = get_bytes(CFFEX_MONTH.format(ym=ym), CFFEX_HEADERS)
            z = zipfile.ZipFile(io.BytesIO(raw))
            n = 0
            for name in z.namelist():
                mt = re.match(r"(\d{8})_1\.csv$", name)
                if not mt:
                    continue
                day = ymd(mt.group(1))
                fr, op = parse_cffex_day(z.read(name))
                main = if_main(fr, day)
                if main:
                    rows[day] = list(main) + [iv_30d(day, fr, op)]
                    n += 1
            months_done.add(ym)
            fetched += 1
            failures = 0
            log(f"  CFFEX {ym}: {n} days, {len(raw) // 1024} KB in {time.monotonic() - t0:.1f}s")
            save()
        except Exception as e:
            failures += 1
            log(f"  CFFEX {ym} failed after {time.monotonic() - t0:.0f}s: {type(e).__name__}: {e}")
        pause()
    left = sum(1 for ym in todo if ym not in months_done)
    if left:
        why = ("time budget used" if time.monotonic() - t_start >= CFFEX_BUDGET else
               f"{failures} failures in a row" if failures >= CFFEX_MAX_FAILURES else "run time budget")
        log(f"  CFFEX: {left} months still to download ({why}); the next run continues")
    save()
    return rows, fetched


def iv_series(if_rows):
    return [(day, r[4]) for day, r in sorted(if_rows.items()) if len(r) > 4 and r[4] is not None and 3 < r[4] < 150]


def basis_series(if_rows, close300):
    """Annualised basis in %: (F / S − 1) × 365 / days to expiry × 100."""
    out = []
    for day in sorted(if_rows):
        if day not in close300:
            continue
        contract, close = if_rows[day][0], if_rows[day][1]
        dte = (if_expiry(contract) - d_of(day)).days
        if dte < IF_MIN_DTE:
            continue
        out.append((day, 100 * (close / close300[day] - 1) * 365 / dte))
    return out


def fg_block(comps, trade_dates, closes, sources, bond_tail=()):
    scores = {k: rolling_scores(v, k in FG_INVERT) for k, v in comps.items()}
    keys = [k for k in FG_META if k in comps]
    need = max(2, len(keys) - 1)                 # at most one component may be missing on a day
    sd = {k: sorted(scores[k]) for k in keys}
    daily = []
    for d in trade_dates:
        got = []
        for k in keys:
            i = bisect.bisect_right(sd[k], d) - 1
            if i >= 0 and (d_of(d) - d_of(sd[k][i])).days <= 7:
                got.append(scores[k][sd[k][i]])
        if len(got) >= need:
            daily.append((d, sum(got) / len(got), len(got)))
    if not daily:
        raise RuntimeError("not enough history for any component yet")
    d, score, n = daily[-1]
    comp_out = []
    for k in keys:
        name, unit, formula = FG_META[k]
        i = bisect.bisect_right(sd[k], d) - 1
        last_d = sd[k][i] if i >= 0 else None
        val = dict(comps[k]).get(last_d) if last_d else None
        comp_out.append({"key": k, "name": name, "unit": unit, "formula": formula, "source": sources.get(k, ""),
                         "date": last_d, "value": round(val, 3) if val is not None else None,
                         "score": round(scores[k][last_d], 1) if last_d else None,
                         "included": bool(last_d) and (d_of(d) - d_of(last_d)).days <= 7})
    def ago(n_days):
        return round(daily[-1 - n_days][1], 1) if len(daily) > n_days else None
    since = years_before(d_of(d), 3).isoformat()
    cd = sorted(closes)
    def close_on(x):
        i = bisect.bisect_right(cd, x) - 1
        return closes[cd[i]] if i >= 0 else None
    # for the app's intraday recalculation: each component's last FG_WINDOW values up to this day (sorted)
    live = {"as_of": d, "windows": {k: sorted(round(v, 4) for _, v in comps[k][-FG_WINDOW:]) for k in keys},
            "invert": sorted(FG_INVERT & set(keys)), "need": need,
            "leverage": next(({"value": c["value"], "score": c["score"], "date": c["date"]} for c in comp_out if c["key"] == "leverage"), None),
            "bond": [[int(x[0].replace("-", "")), x[1]] for x in bond_tail],
            # each component's latest value/score: the app uses these for anything it cannot recompute live
            "last": {c["key"]: {"value": c["value"], "score": c["score"], "date": c["date"]} for c in comp_out if c["score"] is not None}}
    return {"date": d, "score": round(score, 1), "band": fg_band(score), "included": n, "components": comp_out, "live": live,
            "window_days": FG_WINDOW, "min_days": FG_MIN,
            "ago": {"d1": ago(1), "w1": ago(5), "m1": ago(21), "y1": ago(250)},
            "series": [[int(x[0].replace("-", "")), round(x[1], 1), close_on(x[0])]
                       for x in weekly([x for x in daily if x[0] > since], key=lambda x: x[0])]}


def run_fear_greed(today):
    sources = {}
    _, _, rows300, _ = fetch_csi(FG_STOCK, today)
    pause()
    close300 = {d: c for d, c, _ in rows300 if c}
    amounts, all_close = {}, {}
    for code in FG_TURNOVER:
        try:
            _, full, rows_t, amounts = fetch_csi(code, today)
            all_close = {d: c for d, c, _ in rows_t if c}
            pause()
            if len(amounts) > FG_MIN:
                sources["turnover"] = f"{full or code}（{code}）"
                break
        except Exception as e:
            log(f"  turnover source {code} failed: {type(e).__name__}: {e}")
    bond = {}
    for code in FG_BONDS:
        try:
            _, full, rows, _ = fetch_csi(code, today)
            pause()
            if "债" in (full or "") and len(rows) > FG_MIN:
                bond = {d: c for d, c, _ in rows if c}
                sources["safe_haven"] = f"沪深300 与 {full}（{code}）"
                break
            log(f"  bond index {code}: not usable ({full or 'no name'}, {len(rows)} rows)")
        except Exception as e:
            log(f"  bond index {code} failed: {type(e).__name__}: {e}")
    try:
        margin = fetch_margin()
        sources["leverage"] = "东方财富 融资融券交易统计"
    except Exception as e:
        margin = {}
        log(f"  margin data failed: {type(e).__name__}: {e}")
    sources["momentum"] = "沪深300（中证指数官网）"
    if_rows = {}
    try:
        if_rows, n_req = update_if_history(today)
        log(f"  IF futures: {len(if_rows)} days of the main contract ({n_req} CFFEX files fetched this run)"
            + (f", latest {max(if_rows)} {if_rows[max(if_rows)][0]} close {if_rows[max(if_rows)][1]}" if if_rows else ""))
        sources["basis"] = "中国金融期货交易所 日统计（主力合约 = 持仓量最大、距到期 5 天以上）"
    except Exception as e:
        log(f"  IF futures failed: {type(e).__name__}: {e}")
    comps = fg_series(close300, amounts, bond, margin, all_close)
    bs = basis_series(if_rows, close300)
    if len(bs) >= FG_MIN:
        comps["basis"] = bs
    ivs = iv_series(if_rows)
    if len(ivs) >= FG_MIN:                          # option-implied volatility replaces the realised-volatility proxy
        comps["iv"] = ivs
        comps.pop("volatility", None)
        sources["iv"] = "中国金融期货交易所 日统计（沪深300 股指期权结算价）"
    if ivs:
        log(f"  IO options: 30-day implied volatility on {len(ivs)} days, latest {ivs[-1][0]} = {ivs[-1][1]}%"
            + ("" if len(ivs) >= FG_MIN else f" (need {FG_MIN} days before it replaces realised volatility)"))
    for k, v in comps.items():
        log(f"  component {k}: {len(v)} days, latest {v[-1][0] if v else '--'} = {v[-1][1]:.3f}" if v else f"  component {k}: empty")
    b = fg_block(comps, sorted(close300), close300, sources, sorted(bond.items())[-21:])  # bond closes for the app's 20-day return
    if if_rows:
        last = max(if_rows)
        c = if_rows[last]
        b["live"]["futures"] = {"date": last, "contract": c[0], "close": c[1], "expiry": if_expiry(c[0]).isoformat()}
    log(f"Fear & greed: {b['date']} score {b['score']} ({b['band']}) from {b['included']} components: "
        + ", ".join(f"{c['name']} {c['score']}" for c in b["components"]))
    return b


# ---------------------------------------------------------------- 3) industries

def load_industry_history():
    """{board: {date: (pe, pb)}}, names, state."""
    hist = collections.defaultdict(dict)
    if os.path.isdir(IND_DIR):
        for fn in sorted(os.listdir(IND_DIR)):
            m = re.fullmatch(r"(\d{4}-\d{2})\.json", fn)
            if not m:
                continue
            shard = read_json(os.path.join(IND_DIR, fn), {}) or {}
            for code, rows in (shard.get("rows") or {}).items():
                for day, pe, pb in rows:
                    hist[code][f"{m.group(1)}-{int(day):02d}"] = (pe, pb)
    state = read_json(os.path.join(IND_DIR, "boards.json"), {}) or {}
    return hist, state.get("boards", {})


def save_industry_history(hist, boards, months_touched):
    written = 0
    by_month = collections.defaultdict(lambda: collections.defaultdict(list))
    for code, days in hist.items():
        for d in sorted(days):
            if d[:7] in months_touched:
                pe, pb = days[d]
                by_month[d[:7]][code].append([int(d[8:]), pe, pb])
    for month in sorted(months_touched):
        rows = {c: by_month[month][c] for c in sorted(by_month[month])}
        if rows and write_if_changed(os.path.join(IND_DIR, f"{month}.json"), {"month": month, "rows": rows}, ignore=()):
            written += 1
    write_if_changed(os.path.join(IND_DIR, "boards.json"),
                     {"note": "board list and backfill state for fetch_market.py", "boards": boards}, ignore=())
    return written


def ind_rows(order, page, size=500, flt="", want_count=False):
    url = IND_URL.format(size=size, page=page, order=order, filter=flt)
    j = get_json(url, EM_HEADERS)
    data, pages = em_result(j)
    out = []
    for r in data:
        d, code = ymd(r.get("TRADE_DATE")), str(r.get("BOARD_CODE") or "")
        if d and re.fullmatch(r"\d{3,8}", code):
            pe, pb = num(r.get("PE_TTM")), num(r.get("PB_MRQ"))
            out.append((code, str(r.get("BOARD_NAME") or code)[:12], d,
                        round(pe, 2) if pe is not None else None, round(pb, 3) if pb is not None else None,
                        int(num(r.get("NUM")) or 0), int(num(r.get("LOSS_COUNT")) or 0)))
    return (out, pages, em_count(j)) if want_count else (out, pages)


def update_industries(hist, boards):
    """Fetch new days (all boards) and backfill boards without history. Returns touched months, latest info."""
    touched = set()
    latest_info = {}
    # a) newest rows across all boards (sorted newest first): tells us the board list and the latest day
    last_known = {c: max(days) for c, days in hist.items() if days}
    floor = None
    page, pages, seen = 1, 1, set()
    newest = None
    while page <= min(pages, 15):
        rows, pages = ind_rows(-1, page)
        if not rows:
            break
        key = rows[0][:3]
        if key in seen:
            raise RuntimeError("paging parameter ignored (same page twice)")
        seen.add(key)
        newest = newest or rows[0][2]
        for code, name, d, pe, pb, n, loss in rows:
            if code not in boards:
                boards[code] = {"name": name, "backfilled": None}
            boards[code]["name"] = name
            if d == newest:
                latest_info[code] = (n, loss)
            if boards[code].get("backfilled") and d not in hist[code]:
                hist[code][d] = (pe, pb)
                touched.add(d[:7])
        if page == 1:
            # the oldest "last stored day" among boards that are still published
            known = [last_known[c] for c in latest_info if c in last_known and boards[c].get("backfilled")]
            floor = min(known) if known else None
        oldest = rows[-1][2]
        # stop once this page reaches days we already have for every live board (or, before
        # any backfill, after the first page: the backfill fetches everything anyway)
        if floor is None or oldest <= floor:
            break
        page += 1
        pause()
    log(f"Industries: newest day {newest}, {len(latest_info)} boards on it, {len(boards)} known boards")
    # b) backfill: whole history of each board not yet backfilled, while time allows
    todo = [c for c in sorted(boards) if not boards[c].get("backfilled")]
    done_now, failures = 0, 0
    sizes = list(BACKFILL_PAGE_SIZES)
    for code in todo:
        if time_left() < 60 or failures >= MAX_CONSECUTIVE_FAILURES:
            break
        while True:
            try:
                got = backfill_board(code, sizes[0])
                break
            except Exception as e:
                if len(sizes) > 1:          # big pages refused or capped: use the small size from now on
                    log(f"  page size {sizes[0]} not usable ({type(e).__name__}: {e}); using {sizes[1]}")
                    sizes.pop(0)
                    continue
                got = e
                break
        if isinstance(got, Exception):
            failures += 1
            log(f"  backfill {code} failed: {type(got).__name__}: {got}")
            continue
        hist[code].update(got)
        touched.update(d[:7] for d in got)
        boards[code]["backfilled"] = max(got)
        done_now += 1
        failures = 0
        log(f"  backfilled {code} {boards[code]['name']}: {len(got)} days from {min(got)}")
    remaining = sum(1 for c in boards if not boards[c].get("backfilled"))
    log(f"Industries: backfilled {done_now} boards this run (page size {sizes[0]}); {remaining} still to do")
    return touched, latest_info, newest, remaining


def backfill_board(code, size):
    """Whole history of one board, verified complete against the source's row count."""
    flt = "&filter=" + urllib.parse.quote(f'(BOARD_CODE="{code}")')
    got, page, pages, seen, expected = {}, 1, 1, set(), None
    while page <= min(pages, 20):
        rows, pages, cnt = ind_rows(1, page, size=size, flt=flt, want_count=True)
        expected = expected if expected is not None else cnt
        if not rows:
            break
        if rows[0][:3] in seen:
            raise RuntimeError("paging parameter ignored (same page twice)")
        seen.add(rows[0][:3])
        if any(r[0] != code for r in rows):
            raise RuntimeError("board filter ignored by the source")
        for _, _, d, pe, pb, _, _ in rows:
            got[d] = (pe, pb)
        page += 1
        pause()
    if not got:
        raise RuntimeError("no rows")
    if expected and len(got) < 0.98 * expected:
        raise RuntimeError(f"incomplete: {len(got)} of {expected} rows (page size capped by the source?)")
    return got


def industry_block(hist, boards, latest_info, newest, closes):
    """Per-day percentiles without look-ahead (each day compared with the 10 years up to it)."""
    flags_by_day = collections.defaultdict(lambda: [0, 0])     # date -> [undervalued, eligible]
    latest_rows = []
    for code, days in hist.items():
        if not boards.get(code, {}).get("backfilled") or not days:
            continue
        ds = sorted(days)
        first = d_of(ds[0])
        win_pe, win_pb = [], []                       # sorted values inside the window
        q = collections.deque()                       # (date, pe, pb) in window, oldest first
        for d in ds:
            pe, pb = days[d]
            cur = d_of(d)
            start = years_before(cur, YEARS).isoformat()
            while q and q[0][0] <= start:
                _, ope, opb = q.popleft()
                if ope is not None and ope > 0:
                    del win_pe[bisect.bisect_left(win_pe, ope)]
                if opb is not None and opb > 0:
                    del win_pb[bisect.bisect_left(win_pb, opb)]
            q.append((d, pe, pb))
            if pe is not None and pe > 0:
                bisect.insort(win_pe, pe)
            if pb is not None and pb > 0:
                bisect.insort(win_pb, pb)
            if (cur - first).days / 365.25 < IND_MIN_YEARS:
                continue
            pe_pct = pct_below(win_pe, pe) if pe is not None and pe > 0 else None
            pb_pct = pct_below(win_pb, pb) if pb is not None and pb > 0 else None
            if pb_pct is None:
                continue
            under = pb_pct <= UNDER and (pe_pct is None or pe_pct <= UNDER)
            over = pb_pct >= OVER and (pe_pct is None or pe_pct >= OVER)
            f = flags_by_day[d]
            f[1] += 1
            f[0] += 1 if under else 0
            if d == newest:
                n, loss = latest_info.get(code, (0, 0))
                latest_rows.append({"code": code, "name": boards[code]["name"], "pe": pe, "pb": pb,
                                    "pe_pct": pe_pct, "pb_pct": pb_pct, "under": under, "over": over,
                                    "num": n, "loss": loss, "years": round((cur - first).days / 365.25, 1)})
    if not latest_rows or newest not in flags_by_day:
        raise RuntimeError("no board has enough history for the newest day yet")
    # days on which enough boards were eligible to make a count meaningful (10, or all if fewer exist)
    min_boards = max(1, min(10, flags_by_day[newest][1]))
    days = sorted(d for d, f in flags_by_day.items() if f[1] >= min_boards)
    count, total = flags_by_day[newest]
    shares = sorted(flags_by_day[d][0] / flags_by_day[d][1] for d in days)
    share = count / total
    count_pct = pct_below(shares, share)
    verdict = "" if count_pct is None else "偏多" if count_pct >= 70 else "偏少" if count_pct <= 30 else "适中"
    cdates = sorted(closes)
    def close_on(d):
        i = bisect.bisect_right(cdates, d) - 1
        return closes[cdates[i]] if i >= 0 and (d_of(d) - d_of(cdates[i])).days <= 7 else None
    series = [[int(d.replace("-", "")), flags_by_day[d][0], flags_by_day[d][1], close_on(d)] for d in weekly(days, key=lambda x: x)]
    latest_rows.sort(key=lambda r: (max(r["pb_pct"], r["pe_pct"] if r["pe_pct"] is not None else 0), r["code"]))
    return {"date": newest, "count": count, "total": total, "share": round(100 * share, 1),
            "count_pct": count_pct, "verdict": verdict, "series_from": days[0] if days else newest,
            "rule": {"under": UNDER, "over": OVER, "years": YEARS, "min_years": IND_MIN_YEARS},
            "boards": latest_rows, "series": series}


def run_industries(closes):
    hist, boards = load_industry_history()
    touched, latest_info, newest, remaining = update_industries(hist, boards)
    written = save_industry_history(hist, boards, touched)
    log(f"Industries: wrote {written} monthly history files")
    total_boards = len(boards)
    if remaining and (total_boards - remaining) < 0.9 * total_boards:
        return {"status": "backfilling", "date": newest, "boards_total": total_boards,
                "boards_done": total_boards - remaining}
    b = industry_block(hist, boards, latest_info, newest, closes)
    b["status"] = "ok"
    b["boards_pending"] = remaining          # boards still without history (not counted yet)
    log(f"Industries: {b['date']} {b['count']}/{b['total']} undervalued ({b['share']}%), "
        f"count percentile {b['count_pct']}% → {b['verdict']}")
    return b


# ---------------------------------------------------------------- main

def main():
    global SAMPLE_DIR, ROOT, INDICES_FILE, DATA_DIR, CSI_OUT, MARKET_OUT, IND_DIR
    if len(sys.argv) > 2 and sys.argv[1] == "--from-dir":
        SAMPLE_DIR = sys.argv[2]
        ROOT = sys.argv[3] if len(sys.argv) > 3 else SAMPLE_DIR
        INDICES_FILE = os.path.join(ROOT, "indices.txt")
        DATA_DIR = os.path.join(ROOT, "data")
        CSI_OUT = os.path.join(DATA_DIR, "csi_valuation.json")
        MARKET_OUT = os.path.join(DATA_DIR, "market.json")
        IND_DIR = os.path.join(DATA_DIR, "history", "industry")
    _CSI_CACHE.clear()                                       # the cache is per run
    now = datetime.now(BJ)
    if os.environ.get("MARKET_TODAY"):                      # tests: pretend it is another day
        now = datetime.fromisoformat(os.environ["MARKET_TODAY"]).replace(tzinfo=BJ)
    today, now_text = now.date(), now.strftime("%Y-%m-%d %H:%M:%S")
    previous = read_json(MARKET_OUT, {}) or {}
    market = {"schema": 1, "fetched_at_beijing": now_text,
              "sources": {"erp": "中证指数官网（中证800 滚动市盈率）+ 东方财富（中国10年期国债收益率）",
                          "industries": "东方财富行业估值（东方财富行业板块 PE TTM / PB MRQ）",
                          "fear_greed": "中证指数官网（沪深300、中证全指、债券指数）+ 东方财富（融资融券交易统计）"}}
    ok_any = False

    try:
        csi_raw, csi_ok = run_csi(today, now_text)
        ok_any |= csi_ok
    except Exception as e:
        csi_raw = {}
        log(f"CSI step failed: {type(e).__name__}: {e}")

    try:
        blocks, erp_errors = run_erp(today)
        market["erp"] = blocks[ERP_INDEX]
        market["erp_others"] = [blocks[c] for c, _ in ERP_OTHERS if c in blocks]
        if erp_errors:
            market["erp_others_errors"] = erp_errors
        ok_any = True
    except Exception as e:
        log(f"ERP failed: {type(e).__name__}: {e}")
        market["erp"] = previous.get("erp")
        market["erp_others"] = previous.get("erp_others", [])
        market["erp_error"] = f"{type(e).__name__}: {e}"[:200]

    try:
        market["fear_greed"] = run_fear_greed(today)
        ok_any = True
    except Exception as e:
        log(f"Fear & greed failed: {type(e).__name__}: {e}")
        market["fear_greed"] = previous.get("fear_greed")
        market["fear_greed_error"] = f"{type(e).__name__}: {e}"[:200]

    closes = {d: c for d, c, _ in csi_raw.get(ERP_INDEX, []) if c is not None}
    if not closes and previous.get("erp"):
        closes = {ymd(str(s[0])): s[2] for s in previous["erp"].get("series", []) if s[2] is not None}
    try:
        market["industries"] = run_industries(closes)
        ok_any = True
    except Exception as e:
        log(f"Industries failed: {type(e).__name__}: {e}")
        prev = previous.get("industries")
        market["industries"] = prev if prev and prev.get("status") == "ok" else None
        market["industries_error"] = f"{type(e).__name__}: {e}"[:200]

    if market.get("erp") or market.get("industries") or market.get("fear_greed"):
        log("market.json: " + ("saved" if write_if_changed(MARKET_OUT, market) else "unchanged"))
    log(f"Market step took {time.monotonic() - STARTED:.0f}s")
    if not ok_any:
        log("Every part failed; previous files kept.")
        sys.exit(1)


if __name__ == "__main__":
    main()
