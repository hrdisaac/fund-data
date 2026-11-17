#!/usr/bin/env python3
"""
Probe 9: sources for 恒生指数 (股债性价比 with the US 10-year yield, and the signal monitor). Read-only, writes nothing.
Paste into scripts/probe_sources.py, run "Probe data sources" from the Actions tab, paste the whole log back.

  A) 蛋卷 PE / PB history for Hong Kong indices (weekly?) and whether the dj table lists them
  B) Hang Seng Indexes website data endpoint (daily PE / dividend yield?)
  C) Eastmoney: HSI daily K-line history (prices) and quote fields (PE / PB for an index?)
  D) US 10-year Treasury yield: FRED DGS10 CSV and US Treasury daily yield curve CSV
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(url, headers=None, raw=False):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    h.update(headers or {})
    t0 = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
        body, ctype, code = r.read(), r.headers.get("Content-Type", ""), r.status
    secs = time.monotonic() - t0
    if raw:
        return body, ctype, code, secs
    return json.loads(body.decode("utf-8", "replace")), ctype, code, secs


def safe(label, fn):
    try:
        fn()
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:200]}")
    time.sleep(0.8)


def section(t):
    print("=" * 100)
    print(t)


def day(ts):
    return datetime.fromtimestamp(int(ts) / 1000, BJ).strftime("%Y-%m-%d")


def main():
    print(f"Probe 9 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    DJ = {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"}

    section("A) 蛋卷: is any Hong Kong index in the valuation table? PE / PB history for candidate codes")
    def dj_table():
        j, *_ = get("https://danjuanfunds.com/djapi/index_eva/dj", DJ)
        items = ((j.get("data") or {}).get("items")) or []
        hk = [(x.get("index_code"), x.get("name"), x.get("pe"), x.get("pb"), x.get("date")) for x in items
              if any(k in str(x.get("name")) for k in ("恒生", "港", "HK")) or str(x.get("index_code", "")).startswith("HK")]
        print(f"  dj table: {len(items)} indices; Hong Kong ones: {hk or 'none'}")
    safe("dj table", dj_table)
    for code in ("HKHSI", "HSI", "HKHSCEI", "HKHSTECH"):
        for kind in ("pe", "pb"):
            def run(code=code, kind=kind):
                j, *_ = get(f"https://danjuanfunds.com/djapi/index_eva/{kind}_history/{code}?day=all", DJ)
                d = j.get("data") if isinstance(j, dict) else None
                lst = next((v for v in (d or {}).values() if isinstance(v, list) and v and isinstance(v[0], dict)), None) if isinstance(d, dict) else None
                if not lst:
                    print(f"  {kind} {code}: no list ({json.dumps(j, ensure_ascii=False)[:120]})")
                    return
                ts = sorted(x["ts"] for x in lst if x.get("ts"))
                gaps = sorted({(ts[k + 1] - ts[k]) // 86400000 for k in range(min(len(ts) - 1, 60))})
                print(f"  {kind} {code}: {len(lst)} points {day(ts[0])}…{day(ts[-1])}, spacing (days) {gaps[:6]}; newest {lst[-1]}")
            safe(f"{kind} {code}", run)

    section("B) Hang Seng Indexes website (daily PE / yield?)")
    for url in ("https://www.hsi.com.hk/data/eng/rt/index-series/hsi/performance.do",
                "https://www.hsi.com.hk/data/schi/rt/index-series/hsi/performance.do"):
        def run(url=url):
            body, ctype, code, secs = get(url, {"Referer": "https://www.hsi.com.hk/", "Accept": "application/json"}, raw=True)
            text = body.decode("utf-8", "replace")
            print(f"  {url.split('/data/')[1]}: HTTP {code}, {ctype}, {len(body)} bytes in {secs:.1f}s")
            k = text.find('"indexCode":"HSI"') if '"indexCode":"HSI"' in text else text.find("HSI")
            print(f"      around 'HSI': {text[max(0, k - 50):k + 600]!r}")
        safe(url, run)

    section("C) Eastmoney: HSI daily K-line (prices) and quote fields")
    def em_k():
        j, *_ = get("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=100.HSI&ut=bd1d9ddb04089700cf9c27f6f7426281"
                    "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&beg=19900101&end=20500101&lmt=12000",
                    {"Referer": "https://quote.eastmoney.com/"})
        k = ((j.get("data") or {}).get("klines")) or []
        print(f"  100.HSI daily K: {len(k)} bars; first {k[0] if k else '-'}; last {k[-1] if k else '-'}")
    safe("100.HSI kline", em_k)
    def em_q():
        j, *_ = get("https://push2.eastmoney.com/api/qt/stock/get?secid=100.HSI&ut=bd1d9ddb04089700cf9c27f6f7426281"
                    "&fields=f43,f57,f58,f9,f23,f115,f162,f167,f86", {"Referer": "https://quote.eastmoney.com/"})
        print(f"  100.HSI quote fields (f9/f115/f162 = PE variants, f23/f167 = PB): {j.get('data')}")
    safe("100.HSI quote", em_q)

    section("D) US 10-year Treasury yield")
    def fred():
        body, ctype, code, secs = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", raw=True)
        lines = body.decode("utf-8", "replace").strip().splitlines()
        print(f"  FRED DGS10: HTTP {code}, {len(lines)} lines in {secs:.1f}s; header {lines[0]!r}; first {lines[1]!r}; last 3 {lines[-3:]}")
    safe("FRED", fred)
    def treasury():
        y = datetime.now(BJ).year
        url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all"
               f"?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
        body, ctype, code, secs = get(url, raw=True)
        lines = body.decode("utf-8", "replace").strip().splitlines()
        print(f"  Treasury {y}: HTTP {code}, {len(lines)} lines in {secs:.1f}s; header {lines[0][:160]!r}; newest {lines[1][:160]!r}")
    safe("Treasury", treasury)
    print("=" * 100)
    print("Probe 9 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
