#!/usr/bin/env python3
"""
Probe 17 (read-only): 万得全A / 全A PE median, CSI total-return indices, historical short-term rates.
Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
END = datetime.now(BJ).strftime("%Y%m%d")


def get(url, ref, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def safe(label, fn):
    try:
        fn()
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:140]}")
    time.sleep(0.6)


def csi(code):
    j = json.loads(get(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate=20000101&endDate={END}", "https://www.csindex.com.cn/", 40))
    return j.get("data") or []


def main():
    print(f"Probe 17 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    print("=" * 100 + "\nA) 万得全A (881001): search, prices, quote PE; 全A PE median candidates")
    qids = []
    for q in ("万得全A", "881001", "万得全A指数"):
        def run(q=q):
            j = json.loads(get("https://searchapi.eastmoney.com/api/suggest/get?input=" + urllib.parse.quote(q)
                               + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10", "https://www.eastmoney.com/"))
            rows = [(r.get("Code"), r.get("Name"), r.get("QuoteID"), r.get("SecurityTypeName")) for r in ((j.get("QuotationCodeTable") or {}).get("Data") or [])]
            print(f"  search {q}: {rows[:6]}")
            qids.extend(r[2] for r in rows if r[3] == "指数" and r[2])
        safe(q, run)
    for qid in list(dict.fromkeys(qids + ["2.881001", "1.881001", "0.881001", "90.881001"]))[:6]:
        def run(qid=qid):
            j = json.loads(get(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={qid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
                               "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg=19900101&end=20500101&lmt=10000", "https://quote.eastmoney.com/", 40))
            k = ((j.get("data") or {}).get("klines")) or []
            q = json.loads(get(f"https://push2.eastmoney.com/api/qt/stock/get?secid={qid}&ut=bd1d9ddb04089700cf9c27f6f7426281&fields=f43,f57,f58,f9,f23,f115,f162,f167",
                               "https://quote.eastmoney.com/"))
            print(f"  {qid}: {len(k)} bars {k[0] if k else '-'} … {k[-1] if k else '-'}; quote {q.get('data')}")
        safe(qid, run)
    for url, ref in (("https://legulegu.com/stockdata/a-ttm-lyr", "https://legulegu.com/"),
                     ("https://legulegu.com/api/stock-data/market-ttm-lyr?marketId=5", "https://legulegu.com/stockdata/a-ttm-lyr"),
                     ("https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=000985&startDate=20260101&endDate=" + END, "https://www.csindex.com.cn/")):
        def run(url=url, ref=ref):
            t = get(url, ref, 40)
            print(f"  {url[:70]}: {len(t)} bytes; starts {t[:200]!r}")
        safe(url[:40], run)

    print("=" * 100 + "\nB) CSI total-return indices (vs the price index: the ratio should grow by the dividend yield)")
    for tr, px in (("H00300", "000300"), ("H00905", "000905"), ("H00852", "000852"), ("H00985", "000985"), ("H00016", "000016"),
                   ("H00922", "000922"), ("H00906", "000906"), ("H00932", "000932")):
        def run(tr=tr, px=px):
            a, b = csi(tr), csi(px)
            if not a:
                print(f"  {tr}: no rows")
                return
            pm = {x["tradeDate"]: float(x["close"]) for x in b if x.get("close")}
            both = [(x["tradeDate"], float(x["close"]) / pm[x["tradeDate"]]) for x in a if x.get("close") and x["tradeDate"] in pm]
            yrs = (len(both) / 243) if both else 0
            grow = (both[-1][1] / both[0][1]) ** (1 / yrs) - 1 if yrs > 1 else None
            print(f"  {tr} ({a[-1].get('indexNameCn')}): {len(a)} rows {a[0]['tradeDate']}…{a[-1]['tradeDate']}; vs {px}: total / price "
                  f"{both[0][1]:.3f} → {both[-1][1]:.3f}" + (f", +{grow * 100:.2f}%/yr (≈ dividend yield)" if grow is not None else ""))
        safe(tr, run)

    print("=" * 100 + "\nC) historical short-term rates")
    def yeb():
        t = get(f"https://fund.eastmoney.com/pingzhongdata/000198.js?v={int(time.time())}", "https://fund.eastmoney.com/", 40)
        for var in ("Data_sevenDaysYearIncome", "Data_millionCopiesIncome"):
            m = re.search(var + r"\s*=\s*(\[.*?\]);", t, re.S)
            arr = json.loads(m.group(1)) if m else []
            if arr:
                f = lambda x: (datetime.fromtimestamp(x[0] / 1000, BJ).date().isoformat(), x[1])
                print(f"  余额宝 000198 {var}: {len(arr)} points {f(arr[0])} … {f(arr[-1])}")
            else:
                print(f"  余额宝 000198 {var}: not found")
    safe("余额宝", yeb)
    for url in ("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis?lang=cn&startDate=2025-11-01&endDate=2025-11-20",
                "https://www.shibor.org/ags/ms/cm-u-bk-shibor/ShiborHis?lang=cn&startDate=2025-11-01&endDate=2025-11-20"):
        def run(url=url):
            t = get(url, "https://www.chinamoney.com.cn/", 30)
            print(f"  {url[8:60]}: {len(t)} bytes; starts {t[:220]!r}")
        safe(url[8:40], run)
    print("=" * 100 + "\nProbe 17 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
