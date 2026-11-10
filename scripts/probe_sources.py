#!/usr/bin/env python3
"""
Probe 4: 沪深300 股指期权 (IO) — daily prices at CFFEX and a live quote source.  Read-only; writes nothing.
Run "Probe data sources" from the Actions tab and paste the whole log into the chat.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
NOW = datetime.now(BJ)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CF = {"Referer": "http://www.cffex.com.cn/"}
EM = {"Referer": "https://quote.eastmoney.com/"}
UT = "bd1d9ddb04089700cf9c27f6f7426281"


def get(url, extra, raw=False):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(extra)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
        body = r.read()
        return body if raw else json.loads(body.decode("utf-8", errors="replace"))


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


def cffex_options():
    d = NOW.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    url = f"http://www.cffex.com.cn/sj/hqsj/rtj/{d.strftime('%Y%m')}/{d.strftime('%d')}/{d.strftime('%Y%m%d')}_1.csv"
    text = get(url, CF, raw=True).decode("gbk", errors="replace")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    print(f"  {url}: {len(lines)} lines; header {lines[0]}")
    for prod in ("IO", "MO", "HO", "IF"):
        rows = [l for l in lines if l.startswith(prod)]
        months = sorted({re.match(rf"{prod}(\d{{4}})", l).group(1) for l in rows if re.match(rf"{prod}\d{{4}}", l)})
        print(f"  {prod}: {len(rows)} rows, months {months}")
    ifm = [l.split(",") for l in lines if l.startswith("IF")]
    if ifm:
        near = ifm[0]
        print(f"  near IF: {near[0]} close {near[8]}")
        f = float(near[8])
        io = [l.split(",") for l in lines if l.startswith("IO" + near[0][2:6])]
        io.sort(key=lambda r: abs(float(r[0].split("-")[2]) - f))
        for r in io[:6]:
            print(f"    {','.join(r)}")


def em_markets():
    for m in ("221", "220", "222", "223", "8", "10", "11", "12", "151", "152"):
        def run(m=m):
            url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=40&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:{m}"
                   f"&fields=f12,f13,f14,f2,f18&ut={UT}")
            diff = ((get(url, EM).get("data") or {}).get("diff")) or []
            hits = [r for r in diff if re.search(r"IO|300|沪深|购|沽", f"{r.get('f12')}{r.get('f14')}")]
            print(f"  m:{m}: {len(diff)} rows; first {[(r.get('f12'), r.get('f14')) for r in diff[:4]]}; option-like {[(r.get('f12'), r.get('f14')) for r in hits[:6]]}")
        safe(run)


def em_search():
    d = NOW.date()
    ym = (d.replace(day=28) + timedelta(days=4)).strftime("%y%m")
    for q in (f"IO{ym}", "沪深300股指期权", f"IO{ym}-C"):
        def run(q=q):
            url = ("https://searchapi.eastmoney.com/api/suggest/get?input=" + urllib.parse.quote(q)
                   + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10")
            j = get(url, EM)
            data = ((j.get("QuotationCodeTable") or {}).get("Data")) or []
            print(f"  search {q!r}: {[(x.get('QuoteID'), x.get('Code'), x.get('Name'), x.get('SecurityTypeName')) for x in data[:8]]}")
        safe(run)


def main():
    print(f"Probe 4 at {NOW.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) CFFEX daily file: option rows (IO = 沪深300) and the strikes nearest the futures price")
    safe(cffex_options)
    section("B) Eastmoney: which market lists CFFEX options")
    em_markets()
    section("C) Eastmoney search: IO option codes (QuoteID is the secid for live quotes)")
    em_search()
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
