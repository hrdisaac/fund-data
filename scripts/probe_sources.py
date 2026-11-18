#!/usr/bin/env python3
"""
Probe 10: sources for per-fund signals (第三期). Read-only, writes nothing.
Paste into scripts/probe_sources.py, run "Probe data sources" from the Actions tab, paste the whole log back.

  A) 同花顺 hqcode from the relay (fund.10jqka.com.cn/data/client/myfund/{code}) — known: 001593 → JST320, 008888 → JSH923
  B) tracked index of index funds: Eastmoney mobile API and the F10 page (跟踪标的, 业绩比较基准, 基金类型)
  C) full NAV history from the relay (pingzhongdata)
  D) which industry a stock belongs to (for holdings look-through): Eastmoney quote field f100, 申万 component stocks
"""
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
FUNDS = ["001593", "008888", "110022", "161725", "005827", "159601", "512770", "013416"]   # index C / sector C / active / LOF / active / ETF / ETF / other
STOCKS = [("1", "600519"), ("0", "000858"), ("0", "300750"), ("1", "601318"), ("0", "002594")]


SW_AIA = "http://cacerts.digicert.cn/GeoTrustG2TLSCNRSA4096SHA2562022CA1.crt"


def sw_ctx():
    raw = urllib.request.urlopen(urllib.request.Request(SW_AIA, headers={"User-Agent": UA}), timeout=20).read()
    pem = raw.decode() if b"BEGIN CERTIFICATE" in raw else ssl.DER_cert_to_PEM_cert(raw)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=pem)
    return ctx


def get(url, headers=None, timeout=30, ctx=None):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers or {})
    t0 = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout, context=ctx) as r:
        body, code = r.read(), r.status
    return body.decode("utf-8", "replace"), code, time.monotonic() - t0


def safe(label, fn):
    try:
        fn()
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:180]}")
    time.sleep(0.8)


def section(t):
    print("=" * 100)
    print(t)


def main():
    print(f"Probe 10 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) 同花顺 hqcode (expected: 001593 → JST320, 008888 → JSH923)")
    for c in FUNDS:
        def run(c=c):
            text, code, secs = get(f"https://fund.10jqka.com.cn/data/client/myfund/{c}", {"Referer": "https://fund.10jqka.com.cn/"})
            m = re.search(r'"hqcode"\s*:\s*"([^"]+)"', text)
            n = re.search(r'"name"\s*:\s*"([^"]+)"', text)
            print(f"  {c}: HTTP {code}, {len(text)} bytes in {secs:.1f}s; hqcode {m.group(1) if m else '-'}; name {n.group(1) if n else '-'}; "
                  f"start {text[:120]!r}")
        safe(c, run)

    section("B) tracked index / type / benchmark")
    for c in FUNDS:
        def mob(c=c):
            text, code, secs = get(f"https://fundmobapi.eastmoney.com/FundMApi/FundBaseTypeInformation.ashx?FCODE={c}"
                                   f"&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0", {"Referer": "https://mpservice.com/"})
            j = json.loads(text)
            d = j.get("Datas") or {}
            keep = {k: d.get(k) for k in ("SHORTNAME", "FTYPE", "INDEXCODE", "INDEXNAME", "BENCH", "ESTABDATE", "ENDNAV", "FUNDTYPE") if k in d}
            print(f"  {c} mobile API: HTTP {code} in {secs:.1f}s; {keep or text[:200]!r}")
        safe(c + " mobile", mob)
        def f10(c=c):
            text, code, secs = get(f"https://fundf10.eastmoney.com/jbgk_{c}.html", {"Referer": "https://fundf10.eastmoney.com/"})
            out = {}
            for key in ("跟踪标的", "业绩比较基准", "基金类型"):
                m = re.search(key + r"</th>\s*<td[^>]*>(.*?)</td>", text, re.S)
                out[key] = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:80] if m else None
            print(f"  {c} F10 page: HTTP {code}, {len(text) // 1024} KB in {secs:.1f}s; {out}")
        safe(c + " F10", f10)

    section("C) NAV history from the relay (pingzhongdata)")
    for c in FUNDS[:4]:
        def pz(c=c):
            text, code, secs = get(f"https://fund.eastmoney.com/pingzhongdata/{c}.js?v={int(time.time())}", {"Referer": "https://fund.eastmoney.com/"})
            m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
            a = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
            n = len(json.loads(m.group(1))) if m else 0
            first = json.loads(m.group(1))[0] if m else None
            print(f"  {c}: HTTP {code}, {len(text) // 1024} KB in {secs:.1f}s; {n} NAV points from "
                  f"{datetime.fromtimestamp(first['x'] / 1000, BJ).date() if first else '-'}; ACWorth {'yes' if a else 'no'}")
        safe(c, pz)

    section("D) stock → industry")
    secids = ",".join(f"{m}.{c}" for m, c in STOCKS)
    def em():
        text, code, secs = get(f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&fields=f12,f14,f100,f103&secids={secids}"
                               f"&ut=bd1d9ddb04089700cf9c27f6f7426281", {"Referer": "https://quote.eastmoney.com/"})
        j = json.loads(text)
        print(f"  Eastmoney f100 (industry) / f103 (concepts): {[(x.get('f12'), x.get('f14'), x.get('f100'), str(x.get('f103'))[:40]) for x in ((j.get('data') or {}).get('diff') or [])]}")
    safe("Eastmoney", em)
    for url in ("https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/?swindexcode=801120&page=1&page_size=50",
                "https://www.swsresearch.com/institute-sw/api/index_publish/component/?swindexcode=801120&page=1&page_size=50"):
        def sw(url=url):
            text, code, secs = get(url, {"Referer": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex", "Accept": "application/json"},
                                   timeout=40, ctx=sw_ctx())
            print(f"  申万 {url.split('/api/')[1][:60]}: HTTP {code} in {secs:.1f}s; {text[:300]!r}")
        safe("申万", sw)
    print("=" * 100)
    print("Probe 10 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
