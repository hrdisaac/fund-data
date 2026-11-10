#!/usr/bin/env python3
"""
Probe 3: can we get 沪深300 股指期货 (IF) prices for the futures-basis component?  Read-only; writes nothing.
Run "Probe data sources" from the Actions tab and paste the whole log into the chat.

Checks:
  A) CFFEX daily statistics for a recent trading day (CSV and XML forms)
  B) CFFEX monthly history archive (zip) for a past month: files inside, header, IF rows
  C) Eastmoney: how CFFEX futures are coded (list of contracts), a live quote, and daily history depth
"""
import io
import json
import urllib.error
import urllib.request
import zipfile
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
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
        body = r.read()
        return (r.status, r.headers.get("Content-Type"), body) if raw else json.loads(body.decode("utf-8", errors="replace"))


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


def decode(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="replace"), "utf-8?"


def recent_weekdays(n):
    d, out = NOW.date() - timedelta(days=1), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def cffex_daily():
    for d in recent_weekdays(3):
        ym, dd, ymd = d.strftime("%Y%m"), d.strftime("%d"), d.strftime("%Y%m%d")
        for url in (f"http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/{ymd}_1.csv",
                    f"http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/index.xml"):
            def run(url=url):
                status, ctype, body = get(url, CF, raw=True)
                text, enc = decode(body)
                print(f"  {url}\n    HTTP {status}, {ctype}, {len(body)} bytes, {enc}")
                lines = [l for l in text.splitlines() if l.strip()]
                for l in lines[:2]:
                    print(f"    {l[:300]}")
                if_lines = [l for l in lines if "IF" in l][:4]
                for l in if_lines:
                    print(f"    IF: {l[:300]}")
            safe(run)


def cffex_month():
    m = (NOW.replace(day=1) - timedelta(days=40)).strftime("%Y%m")
    old = (NOW.replace(day=1) - timedelta(days=3 * 365)).strftime("%Y%m")
    for ym in (m, old):
        url = f"http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"

        def run(url=url):
            status, ctype, body = get(url, CF, raw=True)
            print(f"  {url}\n    HTTP {status}, {ctype}, {len(body)} bytes")
            z = zipfile.ZipFile(io.BytesIO(body))
            names = z.namelist()
            print(f"    {len(names)} files, e.g. {names[:4]}")
            text, enc = decode(z.read(names[0]))
            lines = [l for l in text.splitlines() if l.strip()]
            print(f"    {names[0]} ({enc}): header {lines[0][:300]}")
            for l in [l for l in lines if l.startswith("IF")][:4]:
                print(f"    IF: {l[:300]}")
        safe(run)


def em_list():
    def run():
        url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=60&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:8"
               f"&fields=f12,f13,f14,f2,f3,f17,f18,f28&ut={UT}")
        j = get(url, EM)
        diff = (j.get("data") or {}).get("diff") or []
        print(f"  {len(diff)} contracts on market 8")
        for r in [r for r in diff if "IF" in str(r.get("f12", "")) or "沪深" in str(r.get("f14", ""))][:10]:
            print(f"    {json.dumps(r, ensure_ascii=False)}")
        if not diff:
            print(f"  reply: {json.dumps(j, ensure_ascii=False)[:400]}")
    safe(run)


def em_quote_and_history():
    m = NOW.month
    ym = NOW.strftime("%y%m")
    cands = ["8.IFM", f"8.IF{ym}", "8.040120", "8.IF00", "220.IFM"]
    for secid in cands:
        def q(secid=secid):
            url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f60,f86,f169,f170&ut={UT}"
            j = get(url, EM)
            print(f"  quote {secid}: {json.dumps(j.get('data'), ensure_ascii=False)[:300]}")
        safe(q)
        def k(secid=secid):
            url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
                   f"&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=0&end=20500101&lmt=1000&ut={UT}")
            j = get(url, EM)
            ks = (j.get("data") or {}).get("klines") or []
            print(f"  kline {secid}: {len(ks)} days" + (f", first {ks[0][:60]}, last {ks[-1][:60]}" if ks else ""))
        safe(k)


def main():
    print(f"Probe 3 at {NOW.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) CFFEX daily statistics, last 3 weekdays (CSV and XML)")
    cffex_daily()
    section("B) CFFEX monthly history archive (recent month and ~3 years ago)")
    cffex_month()
    section("C1) Eastmoney: CFFEX contracts on market 8 (how IF is coded)")
    em_list()
    section("C2) Eastmoney: quote + daily history for candidate IF codes")
    em_quote_and_history()
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
