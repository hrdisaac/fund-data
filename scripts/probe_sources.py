#!/usr/bin/env python3
"""
Probe 5: 申万一级行业 (SW level-1 industries) — list, daily valuation, history depth, prices, live quotes.
Read-only; writes nothing. Paste it into scripts/probe_sources.py, run "Probe data sources", paste the log.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
NOW = datetime.now(BJ)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SW = {"Referer": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex", "Accept": "application/json"}
EM = {"Referer": "https://quote.eastmoney.com/"}
UT = "bd1d9ddb04089700cf9c27f6f7426281"
SW_BASE = "https://www.swsresearch.com/institute-sw/api"


def get(url, extra):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(extra)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"not JSON (HTTP {r.status}): {body[:300]!r}")


def section(t):
    print("=" * 100)
    print(t)


def safe(fn):
    try:
        fn()
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")


def rows_of(j):
    """SW replies seem to be {code, message, data: {results|list: [...], count}}; print enough to learn the shape."""
    d = j.get("data") if isinstance(j, dict) else None
    if isinstance(d, dict):
        for k in ("results", "list", "items", "rows"):
            if isinstance(d.get(k), list):
                return d[k], d
    if isinstance(d, list):
        return d, {}
    return [], d


def sw_current():
    q = urllib.parse.urlencode({"page": 1, "page_size": 50, "indextype": "一级行业"})
    j = get(f"{SW_BASE}/index_publish/current/?{q}", SW)
    rows, meta = rows_of(j)
    print(f"  top-level keys {list(j)[:6]}; {len(rows)} rows; meta {json.dumps({k: v for k, v in (meta or {}).items() if not isinstance(v, list)}, ensure_ascii=False)[:200]}")
    if rows:
        print(f"  fields: {list(rows[0].keys())}")
        print(f"  first: {json.dumps(rows[0], ensure_ascii=False)[:400]}")
        print(f"  codes/names: {[(r.get('swindexcode') or r.get('l1'), r.get('swindexname') or r.get('l2')) for r in rows][:40]}")
    else:
        print(f"  raw: {json.dumps(j, ensure_ascii=False)[:500]}")


def sw_analysis_day():
    d = NOW.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    ds = d.strftime("%Y%m%d")
    q = urllib.parse.urlencode({"page": 1, "page_size": 50, "index_type": "一级行业", "start_date": ds, "end_date": ds,
                                "type": "DAY", "swindexcode": "all"})
    j = get(f"{SW_BASE}/index_analysis/index_analysis_report/?{q}", SW)
    rows, meta = rows_of(j)
    print(f"  {ds}: {len(rows)} rows; count {(meta or {}).get('count') if isinstance(meta, dict) else '--'}")
    if rows:
        print(f"  fields: {list(rows[0].keys())}")
        for r in rows[:3]:
            print(f"  row: {json.dumps(r, ensure_ascii=False)[:400]}")
    else:
        print(f"  raw: {json.dumps(j, ensure_ascii=False)[:500]}")


def sw_analysis_history(code="801120"):
    q = urllib.parse.urlencode({"page": 1, "page_size": 50, "index_type": "一级行业", "start_date": "20000101",
                                "end_date": NOW.strftime("%Y%m%d"), "type": "DAY", "swindexcode": code})
    j = get(f"{SW_BASE}/index_analysis/index_analysis_report/?{q}", SW)
    rows, meta = rows_of(j)
    cnt = (meta or {}).get("count") if isinstance(meta, dict) else None
    dates = sorted(str(r.get("bargaindate") or r.get("trade_date") or "") for r in rows)
    print(f"  {code}: count {cnt}, page has {len(rows)} rows, dates on this page {dates[:1]} … {dates[-1:]}")
    # last page = the other end of the history
    if cnt and int(cnt) > 50:
        last = (int(cnt) + 49) // 50
        q2 = q.replace("page=1", f"page={last}")
        rows2, _ = rows_of(get(f"{SW_BASE}/index_analysis/index_analysis_report/?{q2}", SW))
        d2 = sorted(str(r.get("bargaindate") or r.get("trade_date") or "") for r in rows2)
        print(f"  last page {last}: dates {d2[:1]} … {d2[-1:]}")
        if rows2:
            print(f"  oldest-end row: {json.dumps(rows2[-1], ensure_ascii=False)[:300]}")


def sw_trend(code="801120"):
    q = urllib.parse.urlencode({"swindexcode": code, "period": "DAY"})
    j = get(f"{SW_BASE}/index_publish/trend/?{q}", SW)
    d = j.get("data") if isinstance(j, dict) else None
    rows = d if isinstance(d, list) else rows_of(j)[0]
    print(f"  {code}: {len(rows)} daily rows")
    if rows:
        print(f"  fields: {list(rows[0].keys())}")
        print(f"  first: {json.dumps(rows[0], ensure_ascii=False)[:250]}")
        print(f"  last:  {json.dumps(rows[-1], ensure_ascii=False)[:250]}")
    else:
        print(f"  raw: {json.dumps(j, ensure_ascii=False)[:400]}")


def em_search():
    for q in ("801120", "申万食品饮料", "801010"):
        def run(q=q):
            url = ("https://searchapi.eastmoney.com/api/suggest/get?input=" + urllib.parse.quote(q)
                   + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10")
            data = ((get(url, EM).get("QuotationCodeTable") or {}).get("Data")) or []
            print(f"  search {q!r}: {[(x.get('QuoteID'), x.get('Code'), x.get('Name'), x.get('SecurityTypeName')) for x in data[:8]]}")
        safe(run)


def em_quote():
    for secid in ("2.801120", "90.801120", "47.801120", "48.801120", "1.801120", "0.801120", "100.801120"):
        def run(secid=secid):
            url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f60,f170&fltt=2&ut={UT}"
            print(f"  quote {secid}: {json.dumps(get(url, EM).get('data'), ensure_ascii=False)[:200]}")
        safe(run)


def main():
    print(f"Probe 5 at {NOW.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) 申万宏源研究: current level-1 industry list (should be 31)")
    safe(sw_current)
    section("B) 申万宏源研究: daily valuation of all level-1 industries on the last weekday")
    safe(sw_analysis_day)
    section("C) 申万宏源研究: valuation history depth for one industry (801120 食品饮料)")
    safe(sw_analysis_history)
    section("D) 申万宏源研究: daily price history (trend) for 801120")
    safe(sw_trend)
    section("E1) Eastmoney search: live quote codes for 申万 indices")
    em_search()
    section("E2) Eastmoney: candidate secids for 801120")
    em_quote()
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
