#!/usr/bin/env python3
"""
Probe 2: settle the details the first probe cut off. Read-only; writes nothing.
Run "Probe data sources" from the Actions tab and paste the whole log into the chat.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
NOW = datetime.now(BJ)
TODAY = NOW.strftime("%Y%m%d")
RECENT = (NOW - timedelta(days=12)).strftime("%Y%m%d")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CSI = {"Referer": "https://www.csindex.com.cn/", "Accept": "application/json"}
EM = {"Referer": "https://data.eastmoney.com/"}
DJ = {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"}


def get(url, extra):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(extra)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def section(title):
    print("=" * 100)
    print(title)


def safe(fn):
    try:
        fn()
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")


def csi_recent(code):
    def run():
        j = get(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate={RECENT}&endDate={TODAY}", CSI)
        rows = j.get("data") or []
        print(f"  {len(rows)} rows")
        if rows:
            print(f"  ALL FIELDS: {list(rows[-1].keys())}")
            print(f"  latest row: {json.dumps(rows[-1], ensure_ascii=False)[:900]}")
    return run


def csi_history_depth():
    j = get(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=000906&startDate=20160101&endDate={TODAY}", CSI)
    rows = j.get("data") or []
    keys = [k for k in (rows[-1].keys() if rows else []) if "pe" in k.lower() or "pb" in k.lower() or "yield" in k.lower() or "div" in k.lower()]
    print(f"  {len(rows)} rows for 中证800; valuation-like fields: {keys}")
    for k in keys:
        vals = [r for r in rows if r.get(k) not in (None, "", 0)]
        print(f"  field {k}: {len(vals)} non-empty; first {vals[0]['tradeDate'] if vals else '--'} = {vals[0].get(k) if vals else '--'}; "
              f"latest {vals[-1]['tradeDate'] if vals else '--'} = {vals[-1].get(k) if vals else '--'}")


def em_report(name, extra_qs="", n=3):
    def run():
        j = get(f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName={name}&columns=ALL&pageSize={n}{extra_qs}", EM)
        res = j.get("result")
        if not res:
            print(f"  ⚠ {j.get('message')}")
            return
        data = res.get("data") or []
        print(f"  pages {res.get('pages')}, count {res.get('count')}")
        if data:
            print(f"  ALL FIELDS: {list(data[0].keys())}")
            for row in data[:n]:
                print(f"  row: {json.dumps(row, ensure_ascii=False)[:700]}")
    return run


def dj_hist(code):
    def run():
        j = get(f"https://danjuanfunds.com/djapi/index_eva/pe_history/{code}?day=all", DJ)
        pts = ((j.get("data") or {}).get("index_eva_pe_growths")) or []
        print(f"  {len(pts)} PE points" + (f"; first {pts[0]}; last {pts[-1]}" if pts else " (none)"))
    return run


def main():
    print(f"Probe 2 at {NOW.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    for code, name in [("000906", "中证800"), ("931151", "光伏产业"), ("932000", "中证2000"), ("000510", "中证A500"),
                       ("399976", "CS新能车"), ("H30269", "红利低波"), ("930713", "人工智能")]:
        section(f"CSI daily data, last 12 days: {name} {code}")
        safe(csi_recent(code))
    section("CSI daily data: how far back do valuation fields go (中证800, from 2016)?")
    safe(csi_history_depth)
    section("Eastmoney market valuation RPT_VALUEMARKET (all fields, latest rows)")
    safe(em_report("RPT_VALUEMARKET", "&sortColumns=TRADE_DATE&sortTypes=-1", n=8))
    section("Eastmoney industry valuation: one industry's history depth (earliest rows)")
    safe(em_report("RPT_VALUEINDUSTRY_DET", '&sortColumns=TRADE_DATE&sortTypes=1&filter=(BOARD_CODE%3D%22016058%22)', n=2))
    section("Eastmoney industry valuation: list of industries on the latest day")
    safe(em_report("RPT_VALUEINDUSTRY_DET", '&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=1', n=1))
    for rpt in ["RPTA_WEB_MARGIN_DAILYTRADE", "RPTA_RZRQ_LSHJ", "RPT_MARGIN_TRADING_MARKET", "RPTA_WEB_RZRQ_LSSH"]:
        section(f"Margin trading report candidate: {rpt}")
        safe(em_report(rpt, "", n=2))
    for code in ["SZ399006", "CSIH30269", "HKHSTECH", "SH000906"]:
        section(f"蛋卷 PE history: {code}")
        safe(dj_hist(code))
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
