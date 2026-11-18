#!/usr/bin/env python3
"""
Probe 13: codes and data for the 港股 dividend / high-dividend / Hang Seng sector indices that some funds track. Read-only.
Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
  A) Eastmoney search by name → code / market   B) CSI index-perf (close + PE)   C) Eastmoney daily K   D) 蛋卷 PE history
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NAMES = ["中证港股通央企红利", "中证港股通高股息投资", "中证高股息策略", "恒生港股通红利", "恒生港股通中国内地企业红利", "恒生消费", "恒生医疗保健",
         "中证红利低波动", "中证全指信息技术", "中证养老产业"]


def get(url, ref, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref, "Accept": "application/json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def main():
    print(f"Probe 13 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    found = {}
    print("=" * 100 + "\nA) Eastmoney search (index results only)")
    for n in NAMES:
        try:
            url = ("https://searchapi.eastmoney.com/api/suggest/get?input=" + urllib.parse.quote(n)
                   + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10")
            j = json.loads(get(url, "https://www.eastmoney.com/"))
            rows = ((j.get("QuotationCodeTable") or {}).get("Data")) or []
            idx = [(r.get("Code"), r.get("Name"), r.get("QuoteID"), r.get("SecurityTypeName")) for r in rows]
            print(f"  {n}: {idx[:6]}")
            for code, name, qid, typ in idx:
                if name and n.replace("中证", "").replace("恒生", "")[:4] in name.replace("中证", "").replace("恒生", "") and qid:
                    found.setdefault(n, (code, qid))
                    break
        except Exception as e:
            print(f"  {n}: ❌ {type(e).__name__}: {str(e)[:120]}")
        time.sleep(0.6)
    print("\nmatched:", found)
    print("=" * 100 + "\nB) CSI index-perf (last rows: close, PE) for 6-character codes")
    end = datetime.now(BJ).strftime("%Y%m%d")
    for n, (code, qid) in found.items():
        if not re.fullmatch(r"[0-9A-Z]{6}", str(code)):
            continue
        try:
            j = json.loads(get(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate=20100101&endDate={end}",
                               "https://www.csindex.com.cn/"))
            d = j.get("data") or []
            pe = [x for x in d if x.get("peg") not in (None, "", 0)]
            print(f"  {n} {code}: {len(d)} rows from {d[0]['tradeDate'] if d else '-'}; PE rows {len(pe)} from {pe[0]['tradeDate'] if pe else '-'}; "
                  f"last {({k: d[-1].get(k) for k in ('tradeDate', 'close', 'peg', 'tradingValue')} if d else '-')}")
        except Exception as e:
            print(f"  {n} {code}: ❌ {type(e).__name__}: {str(e)[:120]}")
        time.sleep(0.6)
    print("=" * 100 + "\nC) Eastmoney daily K by QuoteID")
    for n, (code, qid) in found.items():
        try:
            j = json.loads(get(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={qid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
                               "&fields1=f1,f2,f3&fields2=f51,f53,f57&klt=101&fqt=1&beg=20000101&end=20500101&lmt=10000", "https://quote.eastmoney.com/"))
            k = ((j.get("data") or {}).get("klines")) or []
            print(f"  {n} {qid}: {len(k)} bars; first {k[0] if k else '-'}; last {k[-1] if k else '-'}")
        except Exception as e:
            print(f"  {n} {qid}: ❌ {type(e).__name__}: {str(e)[:120]}")
        time.sleep(0.6)
    print("=" * 100 + "\nD) 蛋卷: is any of them in the valuation table?")
    try:
        j = json.loads(get("https://danjuanfunds.com/djapi/index_eva/dj", "https://danjuanfunds.com/djmodule/value-center"))
        items = ((j.get("data") or {}).get("items")) or []
        hits = [(x.get("index_code"), x.get("name")) for x in items if any(w in str(x.get("name")) for w in ("红利", "股息", "港股", "恒生", "医疗", "消费"))]
        print(f"  {len(items)} indices; related: {hits}")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:120]}")
    print("=" * 100 + "\nProbe 13 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
