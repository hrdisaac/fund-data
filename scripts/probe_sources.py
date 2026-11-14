#!/usr/bin/env python3
"""
Probe 8: CSI index-perf stopped at 2026-11-11 (runs of 11-13 00:xx and 11-14 08:00). Read-only, writes nothing.
Paste into scripts/probe_sources.py, run "Probe data sources" from the Actions tab, paste the whole log back.

  A) CSI index-perf for recent days: last rows, response headers (caching?), with and without a cache-buster
  B) Eastmoney daily K-lines for the same indices: which secid works, and do close / amount match CSI on common days?
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CSI = "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate={s}&endDate={e}"
EM = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
      "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&beg={s}&end=20500101&lmt=30")
# CSI code → Eastmoney secid candidates (1. = Shanghai, 0. = Shenzhen, 2. = CSI)
CODES = {"000300": ["1.000300"], "000001": ["1.000001"], "000905": ["1.000905"], "000852": ["1.000852"],
         "932000": ["2.932000", "1.932000"], "000510": ["1.000510", "2.000510"], "000906": ["1.000906"],
         "000985": ["1.000985", "2.000985"], "000688": ["1.000688"], "930743": ["2.930743"], "H30217": ["2.H30217"],
         "990001": ["2.990001"], "399976": ["0.399976"]}


def get(url, headers):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers)
    t0 = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
        body, hdr = r.read(), dict(r.headers)
    return json.loads(body.decode("utf-8", "replace")), hdr, time.monotonic() - t0


def main():
    now = datetime.now(BJ)
    print(f"Probe 8 at {now.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    s, e = "20261101", now.strftime("%Y%m%d")
    csi_last = {}
    print("=" * 100)
    print("A) CSI index-perf, startDate 20261101")
    for k, code in enumerate(CODES):
        for bust in ([False, True] if k == 0 else [False]):
            url = CSI.format(code=code, s=s, e=e) + (f"&_={int(time.time() * 1000)}" if bust else "")
            try:
                j, hdr, secs = get(url, {"Referer": "https://www.csindex.com.cn/", "Accept": "application/json"})
                rows = sorted((r for r in (j.get("data") or []) if isinstance(r, dict)), key=lambda r: str(r.get("tradeDate")))
                tail = [(r.get("tradeDate"), r.get("close"), r.get("peg"), r.get("tradingValue")) for r in rows[-4:]]
                print(f"  {code}{' (cache-buster)' if bust else ''}: {len(rows)} rows in {secs:.1f}s, last 4 (date, close, PE, amount): {tail}")
                if k == 0:
                    keep = {x: hdr.get(x) for x in ("Date", "Last-Modified", "Age", "Cache-Control", "Expires", "ETag", "X-Cache",
                                                    "X-Cache-Lookup", "Via", "Server", "CF-Cache-Status") if hdr.get(x)}
                    print(f"      headers: {keep}")
                for r in rows:
                    csi_last.setdefault(code, {})[str(r.get("tradeDate"))] = (r.get("close"), r.get("tradingValue"))
            except Exception as ex:
                print(f"  {code}: ❌ {type(ex).__name__}: {str(ex)[:150]}")
            time.sleep(0.8)
    print("=" * 100)
    print("B) Eastmoney daily K for the same indices (last 30 bars from 2026-10-01); ratio = Eastmoney ÷ CSI on common days")
    for code, cands in CODES.items():
        for secid in cands:
            try:
                j, _, secs = get(EM.format(secid=secid, s="20261001"), {"Referer": "https://quote.eastmoney.com/"})
                d = j.get("data") or {}
                k = d.get("klines") or []
                if not k:
                    print(f"  {code} via {secid}: no bars (rc {j.get('rc')})")
                    time.sleep(0.5)
                    continue
                last = k[-1].split(",")
                cmp = []
                for line in k:
                    p = line.split(",")
                    key = p[0].replace("-", "")
                    if key in csi_last.get(code, {}):
                        cc, ca = csi_last[code][key]
                        try:
                            cmp.append(f"{p[0]} close {float(p[2]) / float(cc):.5f}, amount {float(p[6]) / 1e8 / float(ca):.3f}")
                        except Exception:
                            pass
                print(f"  {code} via {secid} {d.get('name')}: {len(k)} bars, last {last[0]} close {last[2]} amount {float(last[6]) / 1e8:.1f} 亿")
                print(f"      vs CSI on common days: {cmp[-3:] if cmp else 'none'}")
                break
            except Exception as ex:
                print(f"  {code} via {secid}: ❌ {type(ex).__name__}: {str(ex)[:150]}")
            time.sleep(0.5)
    print("=" * 100)
    print("Probe 8 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
